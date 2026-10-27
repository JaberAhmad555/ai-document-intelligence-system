from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.documents import HealthResponse


router = APIRouter(prefix="/api", tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check API process liveness",
)
def get_health(
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    """Confirm that the API can respond.

    This does not assert database readiness, worker availability,
    extraction success, or completion of database migrations.
    """
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
        check="api_liveness",
    )