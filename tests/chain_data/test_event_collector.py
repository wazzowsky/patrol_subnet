import asyncio
import pytest
from unittest.mock import AsyncMock
from datetime import datetime

from patrol.chain_data.event_collector import EventCollector
from patrol.constants import Constants


@pytest.fixture
def mock_dependencies():
    """Create mock dependencies for the EventCollector."""
    event_fetcher = AsyncMock()
    event_processor = AsyncMock()
    event_repository = AsyncMock()
    missed_block_repository = AsyncMock()

    return {
        "event_fetcher": event_fetcher,
        "event_processor": event_processor,
        "event_repository": event_repository,
        "missed_block_repository": missed_block_repository
    }


@pytest.fixture
def event_collector(mock_dependencies):
    """Create an EventCollector instance with mock dependencies."""
    collector = EventCollector(
        event_fetcher=mock_dependencies["event_fetcher"],
        event_processor=mock_dependencies["event_processor"],
        event_repository=mock_dependencies["event_repository"],
        missed_blocks_repository=mock_dependencies["missed_block_repository"],
        sync_interval=0.1  # Short interval for testing
    )
    return collector


@pytest.mark.asyncio
async def test_event_collector_initialization(event_collector, mock_dependencies):
    """Test that the EventCollector initializes correctly, including new defaults."""
    assert event_collector.event_fetcher is mock_dependencies["event_fetcher"]
    assert event_collector.event_processor is mock_dependencies["event_processor"]
    assert event_collector.event_repository is mock_dependencies["event_repository"]
    assert event_collector.sync_interval == 0.1

    # new defaults
    assert event_collector.batch_size == 50
    assert event_collector.buffer_size == 5000

    assert event_collector.running is False
    assert event_collector.last_synced_block is None


@pytest.mark.asyncio
async def test_convert_to_db_format_transfer_event(event_collector):
    """Test converting a transfer event to database format."""
    event = {
        "coldkey_source": "source_key",
        "coldkey_destination": "dest_key",
        "category": "balance",
        "type": "transfer",
        "evidence": {
            "block_number": 123456,
            "rao_amount": 1000000
        }
    }

    result = event_collector._convert_to_db_format(event)

    assert result["coldkey_source"] == "source_key"
    assert result["coldkey_destination"] == "dest_key"
    assert result["edge_category"] == "balance"
    assert result["edge_type"] == "transfer"
    assert result["block_number"] == 123456
    assert result["rao_amount"] == 1000000

    # staking-only fields should not be present
    assert "destination_net_uid" not in result
    assert "delegate_hotkey_source" not in result
    assert "delegate_hotkey_destination" not in result


@pytest.mark.asyncio
async def test_convert_to_db_format_stake_event(event_collector):
    """Test converting a stake event to database format (with staking fields)."""
    event = {
        "coldkey_source": "source_key",
        "coldkey_destination": "dest_key",
        "category": "staking",
        "type": "add",
        "evidence": {
            "block_number": 123456,
            "rao_amount": 2000000,
            "destination_net_uid": 1,
            "alpha_amount": 1000,
            "delegate_hotkey_destination": "dest_hotkey"
        }
    }

    result = event_collector._convert_to_db_format(event)

    assert result["coldkey_source"] == "source_key"
    assert result["coldkey_destination"] == "dest_key"
    assert result["edge_category"] == "staking"
    assert result["edge_type"] == "add"
    assert result["block_number"] == 123456
    assert result["rao_amount"] == 2000000

    # staking-specific
    assert result["destination_net_uid"] == 1
    assert result["alpha_amount"] == 1000
    assert result["delegate_hotkey_destination"] == "dest_hotkey"

    # source_net_uid and delegate_hotkey_source should be present (but None)
    assert "source_net_uid" in result and result["source_net_uid"] is None
    assert "delegate_hotkey_source" in result and result["delegate_hotkey_source"] is None


@pytest.mark.asyncio
async def test_fetch_and_store_events_streaming(event_collector, mock_dependencies):
    """Test that _fetch_and_store_events drives stream_all_events → processing → storing."""
    start_block = 100
    end_block = 105

    # Raw events as they would come from the substrate
    mock_raw = {
        100: [{"event": {"Balances": [{"Transfer": {"from": ["source1"], "to": ["dest1"], "amount": 1000}}]}}],
        103: [{"event": {"SubtensorModule": [{"StakeAdded": [["coldkey2"], ["hotkey2"], 2000, 500, 1]}]}}]
    }

    # What the processor should return
    block_100_processed = [
        {
            "coldkey_source": "source1",
            "coldkey_destination": "dest1",
            "category": "balance",
            "type": "transfer",
            "evidence": {"block_number": 100, "rao_amount": 1000}
        }
    ]
    block_103_processed = [
        {
            "coldkey_source": "coldkey2",
            "coldkey_destination": "dest_coldkey2",
            "category": "staking",
            "type": "add",
            "evidence": {
                "block_number": 103,
                "rao_amount": 2000,
                "delegate_hotkey_destination": "hotkey2",
                "alpha_amount": 500,
                "destination_net_uid": 1
            }
        }
    ]

    # Fake out the streaming: push each block's raw events into the queue, then None to end
    async def fake_stream(block_numbers, queue, missed_blocks, batch_size=None):
        # should receive full range and honor our batch_size default
        assert block_numbers == list(range(start_block, end_block + 1))
        assert batch_size == event_collector.batch_size

        # Simulate some missed blocks (101, 102, 104, 105)
        missed_blocks.extend([101, 102, 104, 105])

        for blk, ev in mock_raw.items():
            await queue.put({blk: ev})
        await queue.put(None)  # signal completion

    mock_dependencies["event_fetcher"].stream_all_events = AsyncMock(side_effect=fake_stream)

    # Processor now sees the combined dict of both blocks → return both processed lists
    async def fake_process(buffered_dict):
        # ensure both keys are present
        assert set(buffered_dict.keys()) == {100, 103}
        return block_100_processed + block_103_processed

    mock_dependencies["event_processor"].process_event_data = AsyncMock(side_effect=fake_process)

    # Run it
    await event_collector._fetch_and_store_events(start_block, end_block)

    # stream_all_events was awaited once
    mock_dependencies["event_fetcher"].stream_all_events.assert_awaited_once()
    args, kwargs = mock_dependencies["event_fetcher"].stream_all_events.call_args
    assert args[0] == list(range(start_block, end_block + 1))
    assert kwargs["batch_size"] == event_collector.batch_size

    # Processor should have been called exactly once
    mock_dependencies["event_processor"].process_event_data.assert_awaited_once()

    # Repository should have stored exactly 2 converted events
    mock_dependencies["event_repository"].add_events.assert_awaited_once()
    stored = mock_dependencies["event_repository"].add_events.call_args[0][0]
    assert len(stored) == 2

    # Correct data got converted
    assert stored[0]["coldkey_source"] == "source1"
    assert stored[0]["edge_category"] == "balance"
    assert stored[1]["coldkey_source"] == "coldkey2"
    assert stored[1]["edge_category"] == "staking"

    # Should have recorded missed blocks
    mock_dependencies["missed_block_repository"].add_missed_blocks.assert_awaited_once()
    missed_blocks_arg = mock_dependencies["missed_block_repository"].add_missed_blocks.call_args[0][0]
    assert sorted(missed_blocks_arg) == [101, 102, 104, 105]
    assert "Failed fetching blocks!" in mock_dependencies["missed_block_repository"].add_missed_blocks.call_args[1].get("error_message", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("highest_block_in_db,current_block", [
    (400000, 5_000_000),
    (499950,   500_000),
])
async def test_sync_loop_with_existing_blocks(event_collector, mock_dependencies, monkeypatch,
                                             highest_block_in_db, current_block):
    """Test that the sync loop calls _fetch_and_store_events with the right window."""
    # Arrange current/latest
    mock_dependencies["event_fetcher"].get_current_block.return_value = current_block
    mock_dependencies["event_repository"].get_highest_block_from_db.return_value = highest_block_in_db

    # Stub out the actual work so we don't hang on queues
    fake_fetch_store = AsyncMock()
    monkeypatch.setattr(event_collector, "_fetch_and_store_events", fake_fetch_store)

    # Act
    await event_collector.start()
    await asyncio.sleep(0.1)  # allow one iteration
    await event_collector.stop()

    # Assert it was awaited
    fake_fetch_store.assert_awaited_once()
    start_block = highest_block_in_db + 1
    end_block = min(current_block, start_block + 1000)
    called_args = fake_fetch_store.call_args[0]
    assert called_args == (start_block, end_block)

    # And that last_synced_block got updated
    assert event_collector.last_synced_block == end_block


@pytest.mark.asyncio
async def test_sync_loop_with_no_blocks(event_collector, mock_dependencies, monkeypatch):
    """When the DB is empty, we should start at the lower block number limit."""
    current_block = 5_000_000
    mock_dependencies["event_fetcher"].get_current_block.return_value = current_block
    mock_dependencies["event_repository"].get_highest_block_from_db.return_value = None

    fake_fetch_store = AsyncMock()
    monkeypatch.setattr(event_collector, "_fetch_and_store_events", fake_fetch_store)

    await event_collector.start()
    await asyncio.sleep(0.1)
    await event_collector.stop()

    fake_fetch_store.assert_awaited_once()
    start_block = Constants.LOWER_BLOCK_LIMIT
    end_block = min(current_block, start_block + 1000)
    assert fake_fetch_store.call_args[0] == (start_block, end_block)
    assert event_collector.last_synced_block == end_block