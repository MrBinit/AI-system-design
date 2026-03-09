from app.core.config import get_settings
import pybreaker

settings = get_settings()

breaker = pybreaker.CircuitBreaker(
    fail_max=settings.circuit.fail_max,
    reset_timeout=settings.circuit.reset_timeout,
)
