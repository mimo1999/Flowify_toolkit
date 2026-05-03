"""
Resilience and hardening utilities for MCP server.

Provides:
- Circuit breaker for FastAPI calls
- Retry logic with exponential backoff
- Request deduplication
- Health checking
"""
import asyncio
import time
import logging
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from enum import Enum
import hashlib

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker for FastAPI endpoint calls.
    
    Prevents cascading failures by temporarily blocking requests
    to a failing service.
    """
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    half_open_max_calls: int = 3
    
    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    failure_count: int = field(default=0, init=False)
    last_failure_time: float = field(default=0.0, init=False)
    half_open_calls: int = field(default=0, init=False)
    
    def record_success(self):
        """Record successful call."""
        if self.state == CircuitState.HALF_OPEN:
            self.half_open_calls += 1
            if self.half_open_calls >= self.half_open_max_calls:
                logger.info("Circuit breaker: Service recovered, closing circuit")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.half_open_calls = 0
        elif self.state == CircuitState.CLOSED:
            self.failure_count = max(0, self.failure_count - 1)
    
    def record_failure(self):
        """Record failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            logger.warning("Circuit breaker: Service still failing, reopening circuit")
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
        elif self.failure_count >= self.failure_threshold:
            logger.error(f"Circuit breaker: Threshold reached ({self.failure_count} failures), opening circuit")
            self.state = CircuitState.OPEN
    
    def can_attempt(self) -> bool:
        """Check if request can be attempted."""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout elapsed
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                logger.info("Circuit breaker: Recovery timeout elapsed, entering half-open state")
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                return True
            return False
        
        # HALF_OPEN state
        return self.half_open_calls < self.half_open_max_calls
    
    def get_state(self) -> dict:
        """Get current circuit breaker state."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure_time": self.last_failure_time,
            "can_attempt": self.can_attempt()
        }


class RequestDeduplicator:
    """Deduplicate concurrent identical requests.
    
    If multiple identical requests arrive while one is processing,
    they all wait for and share the same result.
    """
    
    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self.pending: dict[str, asyncio.Future] = {}
        self.results: dict[str, tuple[Any, float]] = {}
    
    def _make_key(self, operation: str, **params) -> str:
        """Create cache key from operation and parameters."""
        param_str = str(sorted(params.items()))
        return hashlib.sha256(f"{operation}:{param_str}".encode()).hexdigest()
    
    async def execute(
        self,
        operation: str,
        func: Callable,
        **params
    ) -> Any:
        """Execute function with deduplication."""
        key = self._make_key(operation, **params)
        
        # Check if result is cached
        if key in self.results:
            result, timestamp = self.results[key]
            if time.time() - timestamp < self.ttl:
                logger.debug(f"Request dedup: Cache hit for {operation}")
                return result
            else:
                del self.results[key]
        
        # Check if request is already pending
        if key in self.pending:
            logger.info(f"Request dedup: Waiting for pending {operation}")
            return await self.pending[key]
        
        # Create new future for this request
        future = asyncio.Future()
        self.pending[key] = future
        
        try:
            result = await func(**params)
            self.results[key] = (result, time.time())
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            del self.pending[key]
    
    def clear_cache(self):
        """Clear cached results."""
        self.results.clear()
        logger.info("Request dedup: Cache cleared")


async def retry_with_backoff(
    func: Callable,
    max_retries: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,)
) -> Any:
    """Retry function with exponential backoff.
    
    Args:
        func: Async function to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        backoff_factor: Multiplier for delay after each retry
        exceptions: Tuple of exceptions to catch and retry
    
    Returns:
        Result of successful function call
    
    Raises:
        Last exception if all retries exhausted
    """
    delay = initial_delay
    
    for attempt in range(max_retries + 1):
        try:
            return await func()
        except exceptions as e:
            if attempt == max_retries:
                logger.error(f"Retry exhausted after {max_retries} attempts: {e}")
                raise
            
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)
    
    # Should never reach here, but satisfy type checker
    raise RuntimeError("Retry logic error")


class HealthChecker:
    """Health checker for FastAPI backend."""
    
    def __init__(self, base_url: str, check_interval: float = 30.0):
        self.base_url = base_url
        self.check_interval = check_interval
        self.is_healthy = False
        self.last_check = 0.0
        self.consecutive_failures = 0
    
    async def check_health(self, client) -> bool:
        """Check if FastAPI backend is healthy."""
        try:
            response = await client.get("/", timeout=5.0)
            if response.status_code == 200:
                self.is_healthy = True
                self.consecutive_failures = 0
                logger.debug("Health check: Backend is healthy")
                return True
        except Exception as e:
            logger.warning(f"Health check failed: {e}")
        
        self.is_healthy = False
        self.consecutive_failures += 1
        return False
    
    def should_check(self) -> bool:
        """Check if health check should be performed."""
        return time.time() - self.last_check >= self.check_interval
    
    async def ensure_healthy(self, client) -> bool:
        """Ensure backend is healthy, check if needed."""
        if self.should_check():
            self.last_check = time.time()
            return await self.check_health(client)
        return self.is_healthy
    
    def get_status(self) -> dict:
        """Get health status."""
        return {
            "is_healthy": self.is_healthy,
            "last_check": self.last_check,
            "consecutive_failures": self.consecutive_failures
        }


# Global instances
circuit_breaker = CircuitBreaker()
request_deduplicator = RequestDeduplicator()

# Made with Bob
