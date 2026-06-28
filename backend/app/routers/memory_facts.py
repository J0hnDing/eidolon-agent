from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MemoryFact
from app.schemas.memory_fact import MemoryFactCreate, MemoryFactRead, MemoryFactUpdate


router = APIRouter(prefix="/memory-facts", tags=["memory_facts"])


@router.post("", response_model=MemoryFactRead, status_code=status.HTTP_201_CREATED)
def create_memory_fact(payload: MemoryFactCreate, db: Session = Depends(get_db)) -> MemoryFact:
    memory_fact = MemoryFact(**payload.model_dump())
    db.add(memory_fact)
    db.commit()
    db.refresh(memory_fact)
    return memory_fact


@router.get("", response_model=list[MemoryFactRead])
def list_memory_facts(db: Session = Depends(get_db)) -> list[MemoryFact]:
    return list(db.scalars(select(MemoryFact).order_by(MemoryFact.created_at.desc())).all())


@router.get("/{memory_fact_id}", response_model=MemoryFactRead)
def get_memory_fact(memory_fact_id: int, db: Session = Depends(get_db)) -> MemoryFact:
    memory_fact = db.get(MemoryFact, memory_fact_id)
    if memory_fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory fact not found")
    return memory_fact


@router.patch("/{memory_fact_id}", response_model=MemoryFactRead)
def update_memory_fact(
    memory_fact_id: int,
    payload: MemoryFactUpdate,
    db: Session = Depends(get_db),
) -> MemoryFact:
    memory_fact = db.get(MemoryFact, memory_fact_id)
    if memory_fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory fact not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(memory_fact, key, value)

    db.commit()
    db.refresh(memory_fact)
    return memory_fact


@router.delete("/{memory_fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_fact(memory_fact_id: int, db: Session = Depends(get_db)) -> None:
    memory_fact = db.get(MemoryFact, memory_fact_id)
    if memory_fact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory fact not found")

    db.delete(memory_fact)
    db.commit()
