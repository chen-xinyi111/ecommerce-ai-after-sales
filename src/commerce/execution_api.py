from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from commerce import auth
from commerce import execution_repository as repo
from commerce.execution_models import ExecutionDetail, ExecutionJob, RetryInput
from commerce.models import Principal

router = APIRouter(prefix="/api/v1", tags=["模拟业务执行"])
Actor = Annotated[Principal, Depends(auth.current_actor)]


@router.get("/applications/{app_id}/execution", response_model=ExecutionDetail)
async def execution_detail(app_id: UUID, actor: Actor):
    return await repo.detail(app_id, actor)


@router.post(
    "/applications/{app_id}/retry-execution",
    response_model=ExecutionJob,
    status_code=202,
    dependencies=[Depends(auth.same_origin)],
)
async def retry_execution(app_id: UUID, data: RetryInput, actor: Actor):
    return await repo.enqueue(app_id, data, actor, retry=True)
