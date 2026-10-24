from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.memory_fact import MemoryFactCreate, MemoryFactRead, MemoryFactUpdate
from app.services.agent_memory_service import AgentMemoryService

router = APIRouter(prefix="/memory-facts", tags=["memory_facts"])


@router.post("", response_model=MemoryFactRead, status_code=status.HTTP_201_CREATED)
def create_memory_fact(payload: MemoryFactCreate, db: Session = Depends(get_db)) -> dict:
    service = _memory(db)
    try:
        return service.create_fact({**payload.model_dump(), "provenance_json": {
            "attribution": "stated", "evidence": "Created by the user in Memory",
        }})
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("", response_model=list[MemoryFactRead])
def list_memory_facts(db: Session = Depends(get_db)) -> list[dict]:
    return _memory(db).list_facts()


@router.get("/{memory_fact_id}", response_model=MemoryFactRead)
def get_memory_fact(memory_fact_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return _memory(db).get_fact(memory_fact_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.patch("/{memory_fact_id}", response_model=MemoryFactRead)
def update_memory_fact(
    memory_fact_id: int,
    payload: MemoryFactUpdate,
    db: Session = Depends(get_db),
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if {"key", "value", "category"} & payload.model_fields_set:
        values["provenance_json"] = {"attribution": "stated", "evidence": "Corrected by the user in Memory"}
    try:
        return _memory(db).update_fact(memory_fact_id, values)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/{memory_fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_fact(memory_fact_id: int, db: Session = Depends(get_db)) -> None:
    try:
        _memory(db).delete_fact(memory_fact_id)
    except LookupError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


def _memory(db: Session) -> AgentMemoryService:
    service = AgentMemoryService(db)
    service.migrate_legacy()
    return service
