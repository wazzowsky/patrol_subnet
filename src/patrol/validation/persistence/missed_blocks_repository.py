import logging
from datetime import datetime, UTC
from typing import Dict, List, Optional, Set
import uuid

from sqlalchemy import BigInteger, DateTime, String, distinct, func, select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncEngine
from sqlalchemy.orm import mapped_column, Mapped, MappedAsDataclass

from patrol.validation.persistence import Base

logger = logging.getLogger(__name__)

class MissedBlock(Base, MappedAsDataclass):
    __tablename__ = "missed_blocks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    block_number: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    error_message: Mapped[Optional[str]]

    @classmethod
    def from_block(cls, block_number: int, error_message: Optional[str] = None, retry_count: int = 0):
        """
        Create a MissedBlock object from a block number and optional error information.
        """
        return cls(
            id=str(uuid.uuid4()),
            block_number=block_number,
            created_at=datetime.now(UTC),
            error_message=error_message
        )

class MissedBlocksRepository:
    def __init__(self, engine: AsyncEngine):
        self.LocalAsyncSession = async_sessionmaker(bind=engine)

    async def add_missed_blocks(self, block_numbers: List[int], error_message: Optional[str] = None) -> None:
        """
        Add multiple missed block records. Each call creates new entries.
        
        Args:
            block_numbers: List of block numbers that were missed
            error_message: Optional error context about why the blocks were missed
        """
        missed_blocks = [
            MissedBlock.from_block(block_num, error_message)
            for block_num in block_numbers
        ]
        
        async with self.LocalAsyncSession() as session:
            try:
                session.add_all(missed_blocks)
                await session.commit()
                logger.info(f"Added {len(missed_blocks)} missed blocks to the repository")
            except Exception as e:
                await session.rollback()
                logger.error(f"Error in batch add operation for missed blocks: {e}")
                logger.error(f"Missed blocks: {[block for block in block_numbers]}")

    async def get_all_missed_blocks(self) -> Set[int]:
        """
        Get all unique block numbers that have been missed.
        
        Returns:
            Set of all unique missed block numbers
        """
        async with self.LocalAsyncSession() as session:
            query = select(distinct(MissedBlock.block_number))
            result = await session.execute(query)
            return set(row[0] for row in result.all())
        
    async def remove_blocks(self, block_numbers: List[int]) -> None:
        """
        Remove all records for the specified block numbers from the missed blocks repository.
        
        Args:
            block_numbers: List of block numbers to remove
        """
        if not block_numbers:
            return
            
        async with self.LocalAsyncSession() as session:
            try:
                stmt = delete(MissedBlock).where(MissedBlock.block_number.in_(block_numbers))
                result = await session.execute(stmt)
                await session.commit()
                logger.info(f"Removed {result.rowcount} records for {len(block_numbers)} blocks from missed blocks repository")
            except Exception as e:
                await session.rollback()
                logger.error(f"Error removing blocks from missed blocks repository: {e}")
