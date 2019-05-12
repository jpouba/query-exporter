import asyncio
from collections import defaultdict
import logging

from prometheus_aioexporter import MetricsRegistry
import pytest
import yaml

from ..config import load_config
from ..loop import QueryLoop


@pytest.fixture
def config_data():
    return {
        'databases': {
            'db': {
                'dsn': 'sqlite://'
            }
        },
        'metrics': {
            'm': {
                'type': 'gauge'
            }
        },
        'queries': {
            'q': {
                'interval': 10,
                'databases': ['db'],
                'metrics': ['m'],
                'sql': 'SELECT 100.0'
            },
        }
    }


@pytest.fixture
def registry():
    yield MetricsRegistry()


@pytest.fixture
async def make_query_loop(tmpdir, event_loop, config_data, registry):
    query_loops = []

    def make_query_loop():
        config_file = (tmpdir / 'config.yaml')
        config_file.write_text(yaml.dump(config_data), 'utf-8')
        with config_file.open() as fh:
            config = load_config(fh)
        registry.create_metrics(config.metrics)
        query_loop = QueryLoop(config, registry, logging, event_loop)
        query_loops.append(query_loop)
        return query_loop

    yield make_query_loop
    await asyncio.gather(
        *(query_loop.stop() for query_loop in query_loops), loop=event_loop)


@pytest.fixture
async def query_loop(make_query_loop):
    yield make_query_loop()


def metric_values(metric, by_labels=()):
    """Return values for the metric."""
    if metric._type == 'gauge':
        suffix = ''
    elif metric._type == 'counter':
        suffix = '_total'

    values = defaultdict(list)
    for sample_suffix, labels, value in metric._samples():
        if sample_suffix == suffix:
            if by_labels:
                label_values = tuple(labels[label] for label in by_labels)
                values[label_values] = value
            else:
                values[sample_suffix].append(value)

    return values if by_labels else values[suffix]


@pytest.mark.asyncio
class TestQueryLoop:

    async def test_start(self, query_loop):
        """The start method starts periodic calls for queries."""
        await query_loop.start()
        # self.addCleanup(self.query_loop.stop)
        [periodic_call] = query_loop._periodic_calls
        assert periodic_call.running

    async def test_stop(self, query_loop):
        """The stop method stops periodic calls for queries."""
        await query_loop.start()
        [periodic_call] = query_loop._periodic_calls
        await query_loop.stop()
        assert not periodic_call.running

    async def test_run_query(self, query_loop, registry):
        """Queries are run and update metrics."""
        await query_loop.start()
        await query_loop.stop()
        # the metric is updated
        metric = registry.get_metric('m')
        assert metric_values(metric) == [100.0]
        # the number of queries is updated
        queries_metric = registry.get_metric('queries')
        assert metric_values(
            queries_metric, by_labels=('status', )) == {
                ('success', ): 1.0
            }

    async def test_run_query_null_value(
            self, registry, config_data, make_query_loop):
        """A null value in query results is treated like a zero."""
        config_data['queries']['q']['sql'] = 'SELECT NULL'
        query_loop = make_query_loop()
        await query_loop.start()
        await query_loop.stop()
        metric = registry.get_metric('m')
        assert metric_values(metric) == [0]

    async def test_run_query_log(self, caplog, query_loop):
        """Debug messages are logged on query execution."""
        caplog.set_level(logging.DEBUG)
        await query_loop.start()
        await query_loop.stop()
        assert caplog.messages == [
            'connected to database "db"', 'running query "q" on database "db"',
            'updating metric "m" set(100.0)',
            'updating metric "queries" inc(1)'
        ]

    async def test_run_query_log_error(
            self, caplog, config_data, make_query_loop):
        """Query errors are logged."""
        caplog.set_level(logging.DEBUG)
        config_data['queries']['q']['sql'] = 'WRONG QUERY'
        query_loop = make_query_loop()
        await query_loop.start()
        await query_loop.stop()
        assert (
            'query "q" on database "db" failed: '
            '(sqlite3.OperationalError) near "WRONG": syntax error' in
            caplog.text)

    async def test_run_query_log_invalid_result_count(
            self, caplog, config_data, make_query_loop, registry):
        """An error is logged if result count doesn't match metrics count."""
        caplog.set_level(logging.DEBUG)
        config_data['queries']['q']['sql'] = 'SELECT 100.0, 200.0'
        query_loop = make_query_loop()
        await query_loop.start()
        await query_loop.stop()
        assert (
            'query "q" on database "db" failed: Wrong result count from query'
            in caplog.messages)

    async def test_run_query_increase_db_error_count(
            self, config_data, make_query_loop, registry):
        """Query errors are logged."""
        config_data['databases']['db']['dsn'] = f'sqlite:////invalid'
        query_loop = make_query_loop()
        await query_loop.start()
        await query_loop.stop()
        queries_metric = registry.get_metric('database_errors')
        assert metric_values(queries_metric) == [1.0]

    async def test_run_query_increase_error_count(
            self, config_data, make_query_loop, registry):
        """Count of errored queries is incremented on error."""
        config_data['queries']['q']['sql'] = 'SELECT 100.0 200.0'
        query_loop = make_query_loop()
        await query_loop.start()
        await query_loop.stop()
        queries_metric = registry.get_metric('queries')
        assert metric_values(
            queries_metric, by_labels=('status', )) == {
                ('error', ): 1.0
            }

    async def test_run_query_at_interval(
            self, query_loop, advance_time, tracked_queries):
        """Queries are run at the specified time interval."""
        await query_loop.start()
        await advance_time(0)  # kick the first run
        # the query has been run once
        assert len(tracked_queries) == 1
        await advance_time(5)
        # no more runs yet
        assert len(tracked_queries) == 1
        # now the query runs again
        await advance_time(5)
        assert len(tracked_queries) == 2

    async def test_run_aperiodic_queries(
            self, config_data, make_query_loop, tracked_queries):
        """Queries with null interval can be run explicitly."""
        del config_data['queries']['q']['interval']
        query_loop = make_query_loop()
        await query_loop.run_aperiodic_queries()
        assert len(tracked_queries) == 1
        await query_loop.run_aperiodic_queries()
        assert len(tracked_queries) == 2
