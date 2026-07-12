#!/usr/bin/env python3
"""
G03 Fine-tuning Pipeline — Cloud Run Job for domain-specific model training.

Triggered when a domain accumulates sufficient stable documents (break-even: ~100 docs).
Prepares training data from ingested documents, uploads to provider, and initiates
fine-tuning job. Falls back to RAG for out-of-distribution queries.

Tenant isolation (data-safety): a fine-tune reads ONLY the triggering tenant's document
collection (rag_<tenant>, filtered by the tenant_id payload the doc-pipeline stamps), exports
under finetune-training/<tenant>/<domain>/, and tenant-prefixes its Redis job keys. The
TENANT_ID / QDRANT_COLLECTION values are derived by the trigger (g03_doc_pipeline) and passed
as env — this Job is a standalone Cloud Run image without src/proxy on its path, so it does NOT
import tenancy.context (mirrors the doc-pipeline Job). Under strict BYOK the tenant's own
provider key is passed as TENANT_PROVIDER_KEY; the Job refuses (exit 2) rather than fall back to
the platform key.

Environment Variables:
    TENANT_ID: Owning tenant (default "default" = single-tenant / self-host).
    QDRANT_COLLECTION: Tenant's collection to read (rag_<tenant>; default "rag_docs").
    DOMAIN: Domain identifier (e.g., "customer-support", "legal-contracts")
    DOC_COUNT: Number of documents available for training
    PROVIDER: Provider to use ("vertex_ai" or "openai")
    BASE_MODEL: Base model to fine-tune (e.g., "gpt-4o-mini-2024-07-18")
    GCS_BUCKET: GCS bucket for training data export
    QDRANT_URL: Qdrant vector DB URL (for fetching processed documents)
    REDIS_URL: Redis URL (for tracking training jobs)
    GCP_PROJECT_ID: GCP project for Vertex AI
    GCP_REGION: GCP region for Vertex AI (default: us-central1)
    TENANT_PROVIDER_KEY / BYOK_ENFORCE: Tenant's BYOK training key + strict-BYOK flag.
    OPENAI_API_KEY: Platform OpenAI key — used ONLY for the default/single-tenant case.

Usage:
    # Triggered automatically by G03 doc-pipeline
    python pipeline.py
"""
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# tenant_id sanitiser + redis prefix, inlined to match TenantContext.for_tenant (the Job image
# has no src/proxy on its path). Keep in lock-step with src/proxy/tenancy/context.py.
_TENANT_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def _sanitise_tenant_id(tenant_id: str) -> str:
    tid = _TENANT_ID_DISALLOWED.sub("_", (tenant_id or "").strip())[:64]
    if not tid:
        return "default"
    if not tid[0].isalnum():
        tid = ("t" + tid)[:64]
    return tid


def _redis_prefix(tenant_id: str) -> str:
    safe = _sanitise_tenant_id(tenant_id)
    return f"t:{safe}:" if safe != "default" else ""


@dataclass
class TrainingExample:
    """Single training example for fine-tuning."""
    instruction: str
    input_text: str
    output_text: str
    source_doc_id: str


class TrainingDataBuilder:
    """Build training dataset from ONE tenant's processed documents."""

    def __init__(self, qdrant_url: str, domain: str, tenant_id: str, collection: str):
        self.qdrant_url = qdrant_url
        self.domain = domain
        self.tenant_id = _sanitise_tenant_id(tenant_id)
        # Read the tenant's REAL collection (rag_<tenant>), derived by the trigger and passed
        # as QDRANT_COLLECTION — not the old phantom docs-<domain> that nothing writes to.
        self.collection = collection

    def fetch_documents(self, min_chunks: int = 100) -> List[Dict]:
        """Fetch ONLY this tenant's document chunks from its Qdrant collection."""
        from qdrant_client import QdrantClient

        logger.info(
            "Fetching documents for tenant '%s' from collection: %s",
            self.tenant_id, self.collection,
        )

        client = QdrantClient(url=self.qdrant_url)

        # Defense-in-depth: even though the collection is already tenant-scoped, filter by the
        # tenant_id payload the doc-pipeline stamps, so a mis-pointed collection can never leak
        # another tenant's chunks into this tenant's training corpus.
        scroll_filter = None
        if self.tenant_id != "default":
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                scroll_filter = Filter(must=[
                    FieldCondition(key="tenant_id", match=MatchValue(value=self.tenant_id))
                ])
            except Exception as exc:
                logger.warning("Could not build tenant_id filter (%s) — collection scoping still applies", exc)

        # Scroll to get all points
        all_points = []
        offset = None
        while True:
            response = client.scroll(
                collection_name=self.collection,
                scroll_filter=scroll_filter,
                limit=100,
                offset=offset,
                with_payload=True,
            )
            points, offset = response
            all_points.extend(points)
            if offset is None or len(points) == 0:
                break

        logger.info("Fetched %d document chunks from Qdrant", len(all_points))
        
        if len(all_points) < min_chunks:
            logger.warning(
                "Insufficient chunks: %d < %d minimum",
                len(all_points), min_chunks
            )
        
        return [
            {
                "id": p.id,
                "text": p.payload.get("text", ""),
                "source": p.payload.get("source", ""),
                "metadata": p.payload.get("metadata", {}),
            }
            for p in all_points
        ]
    
    def generate_training_examples(self, chunks: List[Dict]) -> List[TrainingExample]:
        """Generate training examples from document chunks.
        
        Strategy: Create question-answer pairs from chunks using
        a cheap model to generate synthetic questions.
        """
        examples = []
        
        for chunk in chunks:
            text = chunk.get("text", "").strip()
            if len(text) < 100:
                continue
            
            # Create a direct QA pair from the chunk
            # In production, you'd use a model to generate diverse questions
            examples.append(TrainingExample(
                instruction=f"Answer based on the {self.domain} knowledge base.",
                input_text=f"What information is available about: {chunk['metadata'].get('title', 'this topic')}?",
                output_text=text[:2000],  # Limit output length
                source_doc_id=str(chunk["id"]),
            ))
            
            # Add a second variation
            examples.append(TrainingExample(
                instruction=f"Provide details from {self.domain} documentation.",
                input_text=chunk["metadata"].get("title", "Query"),
                output_text=text[:2000],
                source_doc_id=str(chunk["id"]),
            ))
        
        logger.info("Generated %d training examples from %d chunks", len(examples), len(chunks))
        return examples
    
    def export_jsonl(self, examples: List[TrainingExample], output_path: str):
        """Export examples to JSONL format (OpenAI fine-tuning format)."""
        with open(output_path, "w", encoding="utf-8") as f:
            for ex in examples:
                record = {
                    "messages": [
                        {"role": "system", "content": ex.instruction},
                        {"role": "user", "content": ex.input_text},
                        {"role": "assistant", "content": ex.output_text},
                    ]
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        
        logger.info("Exported %d examples to %s", len(examples), output_path)


class VertexAIFineTuner:
    """Vertex AI fine-tuning handler."""
    
    def __init__(self, project_id: str, region: str, gcs_bucket: str):
        self.project_id = project_id
        self.region = region
        self.gcs_bucket = gcs_bucket
    
    def upload_training_data(self, local_path: str, domain: str, tenant_id: str = "default") -> str:
        """Upload training data to GCS under a tenant+domain-nested prefix (isolation)."""
        from google.cloud import storage

        safe_tenant = _sanitise_tenant_id(tenant_id)
        destination = f"finetune-training/{safe_tenant}/{domain}/{int(time.time())}/training.jsonl"

        client = storage.Client(project=self.project_id)
        bucket = client.bucket(self.gcs_bucket)
        blob = bucket.blob(destination)
        
        logger.info("Uploading training data to gs://%s/%s", self.gcs_bucket, destination)
        blob.upload_from_filename(local_path)
        
        return f"gs://{self.gcs_bucket}/{destination}"
    
    def start_finetune_job(
        self,
        training_data_uri: str,
        base_model: str,
        domain: str,
    ) -> str:
        """Start a Vertex AI fine-tuning job."""
        from google.cloud import aiplatform
        
        aiplatform.init(project=self.project_id, location=self.region)
        
        job_display_name = f"finetune-{domain}-{int(time.time())}"
        
        logger.info("Starting Vertex AI fine-tuning job: %s", job_display_name)
        logger.info("  Base model: %s", base_model)
        logger.info("  Training data: %s", training_data_uri)
        
        try:
            # Create tuning job using Vertex AI SDK
            # Note: This uses the experimental tuning API
            model = aiplatform.Model(base_model)
            
            job = model.tune_model(
                training_data_uri=training_data_uri,
                train_steps=100,
                learning_rate_multiplier=1.0,
                tuning_job_display_name=job_display_name,
            )
            
            logger.info("Fine-tuning job started: %s", job.name)
            return job.name
            
        except Exception as exc:
            logger.error("Failed to start fine-tuning job: %s", exc)
            raise


class OpenAIFineTuner:
    """OpenAI fine-tuning handler."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
    
    def upload_training_file(self, local_path: str) -> str:
        """Upload training file to OpenAI."""
        import openai
        
        client = openai.OpenAI(api_key=self.api_key)
        
        logger.info("Uploading training file to OpenAI")
        
        with open(local_path, "rb") as f:
            response = client.files.create(
                file=f,
                purpose="fine-tune",
            )
        
        logger.info("File uploaded: %s", response.id)
        return response.id
    
    def start_finetune_job(
        self,
        training_file_id: str,
        base_model: str,
        domain: str,
    ) -> str:
        """Start an OpenAI fine-tuning job."""
        import openai
        
        client = openai.OpenAI(api_key=self.api_key)
        
        suffix = f"{domain[:10]}-{int(time.time())}"[:18]  # OpenAI limit
        
        logger.info("Starting OpenAI fine-tuning job")
        logger.info("  Base model: %s", base_model)
        logger.info("  Training file: %s", training_file_id)
        logger.info("  Suffix: %s", suffix)
        
        try:
            job = client.fine_tuning.jobs.create(
                training_file=training_file_id,
                model=base_model,
                suffix=suffix,
                hyperparameters={
                    "n_epochs": 3,
                },
            )
            
            logger.info("Fine-tuning job started: %s", job.id)
            return job.id
            
        except Exception as exc:
            logger.error("Failed to start fine-tuning job: %s", exc)
            raise


class FineTunePipeline:
    """Main fine-tuning pipeline orchestrator."""
    
    def __init__(self):
        self.tenant_id = _sanitise_tenant_id(os.getenv("TENANT_ID", "default"))
        # The tenant's real collection, derived by the trigger; falls back to rag_docs for
        # the default/single-tenant case.
        self.collection = os.getenv("QDRANT_COLLECTION", "rag_docs")
        self.redis_prefix = _redis_prefix(self.tenant_id)
        self.domain = os.getenv("DOMAIN", "")
        self.doc_count = int(os.getenv("DOC_COUNT", "0"))
        self.provider = os.getenv("PROVIDER", "vertex_ai")
        self.base_model = os.getenv("BASE_MODEL", "gpt-4o-mini-2024-07-18")
        self.gcs_bucket = os.getenv("GCS_BUCKET", "")
        self.qdrant_url = os.getenv("QDRANT_URL", "http://qdrant-svc:6333")
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.project_id = os.getenv("GCP_PROJECT_ID", "")
        self.region = os.getenv("GCP_REGION", "us-central1")
        # BYOK: the tenant's own training key (passed by the trigger) + strict-BYOK flag.
        # The platform OPENAI_API_KEY is used ONLY for the default/single-tenant case.
        self.tenant_provider_key = os.getenv("TENANT_PROVIDER_KEY", "")
        self.byok_enforce = os.getenv("BYOK_ENFORCE", "false").lower() == "true"
        self.openai_key = os.getenv("OPENAI_API_KEY", "")

    def _resolve_training_key(self) -> str:
        """Return the provider key to train with, fail-closed under strict BYOK.

        A tenant fine-tune must use the TENANT's key — never the platform key. If strict-BYOK
        is on and no tenant key was passed, refuse (exit 2) instead of silently falling back to
        the platform account. For the default/single-tenant case, the platform key is fine.
        """
        if self.tenant_provider_key:
            return self.tenant_provider_key
        if self.byok_enforce or self.tenant_id != "default":
            logger.error(
                "BYOK: no tenant provider key for tenant '%s' — refusing to train on the "
                "platform key (strict isolation). Seed the tenant's key and retry.",
                self.tenant_id,
            )
            sys.exit(2)
        return self.openai_key  # default/single-tenant only

    def _track_job(self, job_id: str, status: str, metadata: Dict):
        """Track fine-tuning job in Redis under the tenant's key namespace."""
        import redis

        try:
            r = redis.from_url(self.redis_url, decode_responses=True)

            job_data = {
                "job_id": job_id,
                "tenant_id": self.tenant_id,
                "domain": self.domain,
                "provider": self.provider,
                "base_model": self.base_model,
                "status": status,
                "started_at": time.time(),
                "metadata": json.dumps(metadata),
            }

            # Store job details + add to the tenant's domain job list — tenant-prefixed so
            # one tenant can never see or overwrite another's fine-tune jobs.
            r.hset(f"{self.redis_prefix}tok_opt:finetune:{job_id}", mapping=job_data)
            r.zadd(f"{self.redis_prefix}tok_opt:finetune:domain:{self.domain}", {job_id: time.time()})

            logger.info("Job tracked in Redis: %s (tenant %s)", job_id, self.tenant_id)

        except Exception as exc:
            logger.warning("Failed to track job in Redis: %s", exc)
    
    def run(self):
        """Execute the full fine-tuning pipeline."""
        logger.info("="*60)
        logger.info("Fine-tuning Pipeline Started")
        logger.info("="*60)
        logger.info("Tenant: %s", self.tenant_id)
        logger.info("Collection: %s", self.collection)
        logger.info("Domain: %s", self.domain)
        logger.info("Documents: %d", self.doc_count)
        logger.info("Provider: %s", self.provider)
        logger.info("Base model: %s", self.base_model)

        if not self.domain:
            logger.error("DOMAIN environment variable required")
            sys.exit(1)

        # BYOK fail-closed guard — resolve the training key up front so we never build a
        # tenant's corpus and then fall back to the platform key at submission.
        training_key = self._resolve_training_key()

        # Step 1: Build training data (tenant-scoped read)
        logger.info("\n[Step 1] Building training dataset...")
        builder = TrainingDataBuilder(self.qdrant_url, self.domain, self.tenant_id, self.collection)
        
        try:
            chunks = builder.fetch_documents(min_chunks=50)
        except Exception as exc:
            logger.error("Failed to fetch documents: %s", exc)
            logger.info("Falling back to direct RAG (no fine-tuning)")
            sys.exit(0)  # Soft fail - RAG still works
        
        if len(chunks) < 50:
            logger.warning("Insufficient documents: %d < 50 minimum", len(chunks))
            logger.info("Skipping fine-tuning - will continue using RAG")
            sys.exit(0)
        
        examples = builder.generate_training_examples(chunks)
        
        if len(examples) < 100:
            logger.warning("Insufficient training examples: %d", len(examples))
            sys.exit(0)
        
        # Export to local file
        training_file = "/tmp/training_data.jsonl"
        builder.export_jsonl(examples, training_file)
        
        # Step 2: Start fine-tuning
        logger.info("\n[Step 2] Starting fine-tuning job...")
        
        try:
            if self.provider == "vertex_ai":
                if not self.project_id:
                    logger.error("GCP_PROJECT_ID required for Vertex AI")
                    sys.exit(1)
                
                tuner = VertexAIFineTuner(self.project_id, self.region, self.gcs_bucket)

                # Upload to GCS under the tenant+domain-nested prefix
                training_uri = tuner.upload_training_data(training_file, self.domain, self.tenant_id)

                # Start job
                job_id = tuner.start_finetune_job(
                    training_uri,
                    self.base_model,
                    self.domain,
                )

            elif self.provider == "openai":
                if not training_key:
                    logger.error("No OpenAI training key resolved")
                    sys.exit(1)

                tuner = OpenAIFineTuner(training_key)  # tenant's BYOK key (or platform for default)
                
                # Upload file
                file_id = tuner.upload_training_file(training_file)
                
                # Start job
                job_id = tuner.start_finetune_job(
                    file_id,
                    self.base_model,
                    self.domain,
                )
                
            else:
                logger.error("Unknown provider: %s", self.provider)
                sys.exit(1)
            
            # Track job
            self._track_job(job_id, "RUNNING", {
                "examples_count": len(examples),
                "chunks_count": len(chunks),
                "training_file": training_file,
            })
            
            logger.info("\n" + "="*60)
            logger.info("Fine-tuning Pipeline Completed Successfully")
            logger.info("="*60)
            logger.info("Job ID: %s", job_id)
            logger.info("Domain: %s", self.domain)
            logger.info("Examples: %d", len(examples))
            
        except Exception as exc:
            logger.error("Fine-tuning job failed: %s", exc)
            sys.exit(1)


def main():
    pipeline = FineTunePipeline()
    pipeline.run()


if __name__ == "__main__":
    main()
