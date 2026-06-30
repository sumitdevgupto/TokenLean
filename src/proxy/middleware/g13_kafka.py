"""
G13 Kafka Integration — Alternative to Redis Streams

Provides Kafka-based batch processing as an alternative to Redis Streams.
For enterprise deployments requiring Kafka infrastructure.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Kafka availability
_kafka_available = False
try:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    _kafka_available = True
except ImportError:
    pass


class KafkaBatchProcessor:
    """Kafka-based batch processing for G13."""
    
    def __init__(self):
        self.brokers = os.getenv("KAFKA_BROKERS", "localhost:9092").split(",")
        self.topic = os.getenv("KAFKA_BATCH_TOPIC", "token-opt-batch-requests")
        self.consumer_group = os.getenv("KAFKA_CONSUMER_GROUP", "token-opt-batch-processor")
        self._producer: Optional[Any] = None
        self._consumer: Optional[Any] = None
        
        if not _kafka_available:
            logger.debug("Kafka not available — using Redis Streams fallback")
    
    async def _get_producer(self) -> Optional[Any]:
        """Lazy-init Kafka producer."""
        if not _kafka_available:
            return None
        
        if self._producer is None:
            try:
                self._producer = AIOKafkaProducer(
                    bootstrap_servers=self.brokers,
                    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                )
                await self._producer.start()
                logger.info("Kafka producer connected to %s", self.brokers)
            except Exception as exc:
                logger.warning("Kafka producer init failed: %s", exc)
                return None
        
        return self._producer
    
    async def enqueue(self, request: Dict[str, Any]) -> bool:
        """Enqueue request to Kafka batch topic."""
        producer = await self._get_producer()
        if not producer:
            return False
        
        try:
            await producer.send(self.topic, request)
            logger.debug("Request enqueued to Kafka: %s", request.get("request_id"))
            return True
        except Exception as exc:
            logger.error("Kafka enqueue failed: %s", exc)
            return False
    
    async def start_consumer(self, handler: callable):
        """Start Kafka consumer for batch processing."""
        if not _kafka_available:
            logger.error("Kafka not available — cannot start consumer")
            return
        
        try:
            self._consumer = AIOKafkaConsumer(
                self.topic,
                bootstrap_servers=self.brokers,
                group_id=self.consumer_group,
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="earliest",
            )
            
            await self._consumer.start()
            logger.info("Kafka consumer started on topic: %s", self.topic)
            
            # Process messages
            async for msg in self._consumer:
                try:
                    logger.debug("Processing Kafka message: %s", msg.key)
                    await handler(msg.value)
                except Exception as exc:
                    logger.error("Message processing failed: %s", exc)
                    
        except Exception as exc:
            logger.error("Kafka consumer error: %s", exc)
        finally:
            await self.stop()
    
    async def stop(self):
        """Stop Kafka producer and consumer."""
        if self._producer:
            await self._producer.stop()
            self._producer = None
        
        if self._consumer:
            await self._consumer.stop()
            self._consumer = None


class G13Kafka:
    """G13 batch processing with Kafka backend option."""
    
    def __init__(self):
        self.kafka = KafkaBatchProcessor()
        self.use_kafka = os.getenv("G13_USE_KAFKA", "false").lower() == "true"
    
    async def process_request(self, ctx: Any) -> Any:
        """Process request with optional Kafka batching."""
        if not self.use_kafka:
            # Use Redis Streams (original G13 implementation)
            return ctx
        
        cfg = ctx.config.get("groups", {}).get("G13_batch", {})
        if not cfg.get("enabled", False):
            return ctx
        
        # Check if batching applies
        if not self._should_batch(ctx):
            return ctx
        
        # Enqueue to Kafka
        request_data = {
            "request_id": ctx.request_id,
            "user_id": ctx.user_id,
            "messages": ctx.messages,
            "model": ctx.model,
            "params": ctx.params,
            "timestamp": time.time(),
        }
        
        success = await self.kafka.enqueue(request_data)
        if success:
            ctx.batch_deferred = True
            logger.info("[%s] G13 request deferred to Kafka batch", ctx.request_id)
        
        return ctx
    
    def _should_batch(self, ctx: Any) -> bool:
        """Determine if request should be batched."""
        # Batch if marked as batch-eligible or if it's a background task
        params = ctx.params
        if params.get("x_batch_mode", False):
            return True
        
        # Check request priority
        priority = params.get("x_priority", "normal")
        if priority == "background":
            return True
        
        return False


import time
