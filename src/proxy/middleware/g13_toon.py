"""
G13 TOON (Token-Optimized Object Notation) — Batch Compression

Implements code-substitution / legend-amortisation pattern for large repeated payloads.
Reduces token usage in batch requests by:
1. Identifying common patterns in payloads
2. Substituting with short codes
3. Transmitting legend once, referencing codes many times
"""
import hashlib
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from middleware import RequestContext

logger = logging.getLogger(__name__)
GROUP = "G13_TOON"


class TOONLegend:
    """
    TOON Legend for code substitution.
    
    Maps short codes (e.g., #P1, #S1) to full values.
    Legend is transmitted once and referenced many times.
    """
    
    def __init__(self):
        self.substitutions: Dict[str, str] = {}
        self.code_counter = 0
    
    def add_substitution(self, value: str, prefix: str = "#C") -> str:
        """
        Add a value to the legend and return its code.
        
        Args:
            value: Full text to substitute
            prefix: Code prefix (e.g., #C for content, #P for prefix, #S for state)
            
        Returns:
            code: Short code to use in place of value
        """
        # Check if already in legend
        for code, existing_value in self.substitutions.items():
            if existing_value == value:
                return code
        
        # Generate new code
        self.code_counter += 1
        code = f"{prefix}{self.code_counter}"
        self.substitutions[code] = value
        
        return code
    
    def get_value(self, code: str) -> Optional[str]:
        """Get original value for a code."""
        return self.substitutions.get(code)
    
    def to_dict(self) -> Dict[str, str]:
        """Export legend as dictionary."""
        return self.substitutions.copy()
    
    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "TOONLegend":
        """Import legend from dictionary."""
        legend = cls()
        legend.substitutions = data.copy()
        legend.code_counter = len(data)
        return legend
    
    def calculate_savings(self) -> Tuple[int, int]:
        """
        Calculate token savings from compression.
        
        Returns:
            (original_tokens, compressed_tokens)
        """
        original = 0
        compressed = 0
        
        for code, value in self.substitutions.items():
            # Original value tokens (rough estimate: 4 chars per token)
            original += len(value) // 4
            # Compressed: code appears once in legend, referenced multiple times
            # We count the code length in legend + one reference
            compressed += (len(code) // 4) + (len(code) // 4)
        
        return original, compressed


class TOONCompressor:
    """Compress batch requests using TOON notation."""
    
    def __init__(self):
        self.min_pattern_length = 20  # Minimum length to warrant substitution
    
    def compress_batch(
        self,
        messages: List[Dict],
        state: Optional[Dict] = None,
    ) -> Tuple[List[Dict], TOONLegend, int]:
        """
        Compress a batch of messages.
        
        Args:
            messages: List of message dictionaries
            state: Optional shared state to compress
            
        Returns:
            (compressed_messages, legend, tokens_saved)
        """
        legend = TOONLegend()
        
        # Find common patterns
        patterns = self._find_common_patterns(messages)
        
        # Add patterns to legend
        pattern_codes = {}
        for pattern in patterns:
            if len(pattern) >= self.min_pattern_length:
                code = legend.add_substitution(pattern, "#P")
                pattern_codes[pattern] = code
        
        # Add state to legend if present
        state_code = None
        if state:
            state_json = json.dumps(state, sort_keys=True, separators=(',', ':'))
            if len(state_json) > 50:
                state_code = legend.add_substitution(state_json, "#S")
        
        # Compress messages
        compressed_messages = []
        for msg in messages:
            compressed_msg = self._compress_message(msg, pattern_codes, state_code)
            compressed_messages.append(compressed_msg)
        
        # Calculate savings
        original_tokens = sum(
            len(json.dumps(m)) // 4 for m in messages
        )
        compressed_tokens = sum(
            len(json.dumps(m)) // 4 for m in compressed_messages
        )
        legend_tokens = len(json.dumps(legend.to_dict())) // 4
        
        total_compressed = compressed_tokens + legend_tokens
        tokens_saved = max(0, original_tokens - total_compressed)
        
        return compressed_messages, legend, tokens_saved
    
    def decompress_batch(
        self,
        compressed_messages: List[Dict],
        legend: TOONLegend,
    ) -> List[Dict]:
        """
        Decompress messages using legend.
        
        Args:
            compressed_messages: Compressed message list
            legend: TOON legend with substitutions
            
        Returns:
            Original messages
        """
        return [
            self._decompress_message(msg, legend)
            for msg in compressed_messages
        ]
    
    def _find_common_patterns(self, messages: List[Dict]) -> List[str]:
        """Find common string patterns across messages."""
        # Extract all string values
        all_strings = []
        for msg in messages:
            all_strings.extend(self._extract_strings(msg))
        
        # Find common prefixes
        patterns = []
        if len(all_strings) > 1:
            common_prefix = self._longest_common_prefix(all_strings)
            if len(common_prefix) >= self.min_pattern_length:
                patterns.append(common_prefix)
        
        # Find repeated substrings (simplified)
        from collections import Counter
        substring_counts = Counter()
        
        for s in all_strings:
            # Extract substrings of various lengths
            for length in [50, 100, 200]:
                if len(s) >= length:
                    # Sample substrings
                    for i in range(0, min(len(s) - length, 500), length):
                        substring = s[i:i+length]
                        substring_counts[substring] += 1
        
        # Keep substrings that appear in multiple messages
        for substr, count in substring_counts.most_common(10):
            if count >= 2 and len(substr) >= self.min_pattern_length:
                patterns.append(substr)
        
        return patterns
    
    def _extract_strings(self, obj) -> List[str]:
        """Recursively extract all strings from a dict/list."""
        strings = []
        if isinstance(obj, dict):
            for v in obj.values():
                strings.extend(self._extract_strings(v))
        elif isinstance(obj, list):
            for item in obj:
                strings.extend(self._extract_strings(item))
        elif isinstance(obj, str):
            strings.append(obj)
        return strings
    
    def _longest_common_prefix(self, strings: List[str]) -> str:
        """Find longest common prefix among strings."""
        if not strings:
            return ""
        
        prefix = strings[0]
        for s in strings[1:]:
            while not s.startswith(prefix):
                prefix = prefix[:-1]
                if not prefix:
                    break
        return prefix
    
    def _compress_message(
        self,
        msg: Dict,
        pattern_codes: Dict[str, str],
        state_code: Optional[str],
    ) -> Dict:
        """Compress a single message."""
        # Convert to JSON for pattern substitution
        json_str = json.dumps(msg)
        
        # Substitute patterns
        for pattern, code in sorted(pattern_codes.items(), key=lambda x: -len(x[0])):
            json_str = json_str.replace(pattern, code)
        
        # Substitute state reference
        if state_code and "x-token-opt-state" in json_str:
            # Replace full state with code
            json_str = re.sub(
                r'"x-token-opt-state":\s*"[^"]+"',
                f'"x-token-opt-state": "{state_code}"',
                json_str,
            )
        
        # Parse back to dict
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return msg  # Return original if compression corrupted
    
    def _decompress_message(self, msg: Dict, legend: TOONLegend) -> Dict:
        """Decompress a single message."""
        json_str = json.dumps(msg)
        
        # Replace codes with values
        for code, value in legend.substitutions.items():
            json_str = json_str.replace(code, value)
        
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return msg


class G13TOONBatch:
    """G13 TOON batch compression middleware."""
    
    def __init__(self):
        self._compressor = TOONCompressor()
    
    async def process_batch_request(
        self,
        ctx: RequestContext,
        messages: List[Dict],
    ) -> Tuple[List[Dict], Optional[TOONLegend], int]:
        """
        Process batch request with TOON compression.
        
        Returns:
            (processed_messages, legend_or_none, tokens_saved)
        """
        cfg = ctx.config.get("groups", {}).get("G13_batch", {})
        if not cfg.get("toon_enabled", False):
            return messages, None, 0
        
        try:
            # Extract state for compression
            state = None
            if hasattr(ctx, 'params'):
                state = ctx.params.get("token_opt_state")
            
            # Compress
            compressed, legend, savings = self._compressor.compress_batch(messages, state)
            
            if savings > 0:
                ctx.savings.add_step(
                    GROUP,
                    f"TOON compression: saved {savings} tokens",
                    len(messages) * 100,  # Rough estimate
                    len(messages) * 100 - savings,
                )
                
                logger.debug(
                    "[%s] G13 TOON: compressed %d messages, saved %d tokens",
                    ctx.request_id,
                    len(messages),
                    savings,
                )
            
            return compressed, legend, savings
            
        except Exception as exc:
            logger.warning("[%s] G13 TOON compression failed: %s", ctx.request_id, exc)
            return messages, None, 0
    
    async def decompress_batch_response(
        self,
        ctx: RequestContext,
        compressed_messages: List[Dict],
        legend: TOONLegend,
    ) -> List[Dict]:
        """Decompress batch response."""
        try:
            return self._compressor.decompress_batch(compressed_messages, legend)
        except Exception as exc:
            logger.warning("[%s] G13 TOON decompression failed: %s", ctx.request_id, exc)
            return compressed_messages


def calculate_batch_token_cap(
    messages: List[Dict],
    config: Dict,
) -> Optional[int]:
    """
    Calculate token budget cap for batch before flush.
    
    Prevents accumulated batches from exceeding budget.
    """
    cfg = config.get("groups", {}).get("G13_batch", {})
    if not cfg.get("batch_token_cap_enabled", False):
        return None
    
    # Estimate tokens in batch
    total_estimated = 0
    for msg in messages:
        # Rough estimation: 4 chars per token for JSON
        json_str = json.dumps(msg)
        total_estimated += len(json_str) // 4
    
    cap = cfg.get("batch_token_cap", 10000)
    
    if total_estimated > cap:
        logger.warning(
            "Batch exceeds token cap: %d > %d — forcing flush",
            total_estimated,
            cap,
        )
        return 0  # Signal to flush immediately
    
    return cap - total_estimated  # Remaining budget


if __name__ == "__main__":
    # Test TOON compression
    compressor = TOONCompressor()
    
    messages = [
        {
            "role": "user",
            "content": "The quick brown fox jumps over the lazy dog. Question 1?",
        },
        {
            "role": "user",
            "content": "The quick brown fox jumps over the lazy dog. Question 2?",
        },
        {
            "role": "user",
            "content": "The quick brown fox jumps over the lazy dog. Question 3?",
        },
    ]
    
    compressed, legend, savings = compressor.compress_batch(messages)
    
    print(f"Original messages: {len(messages)}")
    print(f"Legend entries: {len(legend.substitutions)}")
    print(f"Tokens saved: {savings}")
    print(f"Legend: {legend.to_dict()}")
