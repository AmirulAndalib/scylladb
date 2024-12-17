#
# Copyright (C) 2024-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import asyncio
import pytest

from test.pylib.internal_types import ServerInfo
from test.pylib.manager_client import ManagerClient
from test.pylib.repair import create_table_insert_data_for_repair
from test.pylib.tablets import get_all_tablet_replicas
from test.topology.conftest import skip_mode
from test.topology_experimental_raft.test_tablets import inject_error_on
from test.topology_tasks.task_manager_client import TaskManagerClient
from test.topology_tasks.task_manager_types import TaskStatus, TaskStats

async def enable_injection(manager: ManagerClient, servers: list[ServerInfo], injection: str):
    for server in servers:
        await manager.api.enable_injection(server.ip_addr, injection, False)

async def disable_injection(manager: ManagerClient, servers: list[ServerInfo], injection: str):
    for server in servers:
        await manager.api.disable_injection(server.ip_addr, injection)

async def wait_tasks_created(tm: TaskManagerClient, server: ServerInfo, module_name: str, expected_number: int, type: str):
    async def get_tasks():
        return [task for task in await tm.list_tasks(server.ip_addr, module_name) if task.kind == "cluster" and task.type == type and task.keyspace == "test"]

    tasks = await get_tasks()
    while len(tasks) != expected_number:
        tasks = await get_tasks()
    return tasks

def check_task_status(status: TaskStatus, states: list[str], type: str, scope: str, abortable: bool):
    assert status.scope == scope
    assert status.kind == "cluster"
    assert status.type == type
    assert status.keyspace == "test"
    assert status.table == "test"
    assert status.is_abortable == abortable
    assert not status.children_ids
    assert status.state in states

async def check_and_abort_repair_task(tm: TaskManagerClient, servers: list[ServerInfo], module_name: str):
    # Wait until user repair task is created.
    repair_tasks = await wait_tasks_created(tm, servers[0], module_name, 1, "user_repair")

    task = repair_tasks[0]
    assert task.scope == "table"
    assert task.keyspace == "test"
    assert task.table == "test"
    assert task.state in ["created", "running"]

    status = await tm.get_task_status(servers[0].ip_addr, task.task_id)

    check_task_status(status, ["created", "running"], "user_repair", "table", True)

    async def wait_for_task():
        status_wait = await tm.wait_for_task(servers[0].ip_addr, task.task_id)
        check_task_status(status_wait, ["done"], "user_repair", "table", True)

    async def abort_task():
        await tm.abort_task(servers[0].ip_addr, task.task_id)

    await asyncio.gather(wait_for_task(), abort_task())

@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_tablet_repair_task(manager: ManagerClient):
    module_name = "tablets"
    tm = TaskManagerClient(manager.api)

    servers, cql, hosts, table_id = await create_table_insert_data_for_repair(manager)
    assert module_name in await tm.list_modules(servers[0].ip_addr), "tablets module wasn't registered"

    async def repair_task():
        token = -1
        # Keep retring tablet repair.
        await inject_error_on(manager, "repair_tablet_fail_on_rpc_call", servers)
        await manager.api.tablet_repair(servers[0].ip_addr, "test", "test", token)

    await asyncio.gather(repair_task(), check_and_abort_repair_task(tm, servers, module_name))

async def check_repair_task_list(tm: TaskManagerClient, servers: list[ServerInfo], module_name: str):
    def get_task_with_id(repair_tasks, task_id):
        tasks_with_id1 = [task for task in repair_tasks if task.task_id == task_id]
        assert len(tasks_with_id1) == 1
        return tasks_with_id1[0]

    # Wait until user repair tasks are created.
    repair_tasks0 = await wait_tasks_created(tm, servers[0], module_name, len(servers), "user_repair")
    repair_tasks1 = await wait_tasks_created(tm, servers[1], module_name, len(servers), "user_repair")
    repair_tasks2 = await wait_tasks_created(tm, servers[2], module_name, len(servers), "user_repair")

    assert len(repair_tasks0) == len(repair_tasks1), f"Different number of repair virtual tasks on nodes {servers[0].server_id} and {servers[1].server_id}"
    assert len(repair_tasks0) == len(repair_tasks2), f"Different number of repair virtual tasks on nodes {servers[0].server_id} and {servers[2].server_id}"

    for task0 in repair_tasks0:
        task1 = get_task_with_id(repair_tasks1, task0.task_id)
        task2 = get_task_with_id(repair_tasks2, task0.task_id)

        assert task0.table in ["test", "test2", "test3"]
        assert task0.table == task1.table, f"Inconsistent table for task {task0.task_id}"
        assert task0.table == task2.table, f"Inconsistent table for task {task0.task_id}"

        for task in [task0, task1, task2]:
            assert task.state in ["created", "running"]
            assert task.type == "user_repair"
            assert task.kind == "cluster"
            assert task.scope == "table"
            assert task.keyspace == "test"

        await tm.abort_task(servers[0].ip_addr, task0.task_id)

@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_tablet_repair_task_list(manager: ManagerClient):
    module_name = "tablets"
    tm = TaskManagerClient(manager.api)

    servers, cql, hosts, table_id = await create_table_insert_data_for_repair(manager)
    assert module_name in await tm.list_modules(servers[0].ip_addr), "tablets module wasn't registered"

    # Create other tables.
    await cql.run_async("CREATE TABLE test.test2 (pk int PRIMARY KEY, c int) WITH tombstone_gc = {'mode':'repair'};")
    await cql.run_async("CREATE TABLE test.test3 (pk int PRIMARY KEY, c int) WITH tombstone_gc = {'mode':'repair'};")
    keys = range(256)
    await asyncio.gather(*[cql.run_async(f"INSERT INTO test.test2 (pk, c) VALUES ({k}, {k});") for k in keys])
    await asyncio.gather(*[cql.run_async(f"INSERT INTO test.test3 (pk, c) VALUES ({k}, {k});") for k in keys])

    async def run_repair(server_id, table_name):
        token = -1
        await manager.api.tablet_repair(servers[server_id].ip_addr, "test", table_name, token)

    await inject_error_on(manager, "repair_tablet_fail_on_rpc_call", servers)

    await asyncio.gather(run_repair(0, "test"), run_repair(1, "test2"), run_repair(2, "test3"), check_repair_task_list(tm, servers, module_name))

async def prepare_migration_test(manager: ManagerClient):
    servers = []
    host_ids = []

    async def make_server():
        s = await manager.server_add()
        servers.append(s)
        host_ids.append(await manager.get_host_id(s.server_id))
        await manager.api.disable_tablet_balancing(s.ip_addr)

    await make_server()
    cql = manager.get_cql()
    await cql.run_async("CREATE KEYSPACE test WITH replication = {'class': 'NetworkTopologyStrategy', 'replication_factor': 1} AND tablets = {'initial': 1}")
    await cql.run_async("CREATE TABLE test.test (pk int PRIMARY KEY, c int);")
    await make_server()

    await cql.run_async(f"INSERT INTO test.test (pk, c) VALUES ({1}, {1});")

    return (servers, host_ids)

@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_tablet_migration_task(manager: ManagerClient):
    module_name = "tablets"
    tm = TaskManagerClient(manager.api)
    servers, host_ids = await prepare_migration_test(manager)

    injection = "handle_tablet_migration_end_migration"

    async def move_tablet(old_replica, new_replica):
        await manager.api.enable_injection(servers[0].ip_addr, injection, False)
        await manager.api.move_tablet(servers[0].ip_addr, "test", "test", old_replica[0], old_replica[1], new_replica[0], new_replica[1], 0)

    async def check(type):
        # Wait until migration task is created.
        migration_tasks = await wait_tasks_created(tm, servers[0], module_name, 1, type)

        assert len(migration_tasks) == 1
        status = await tm.get_task_status(servers[0].ip_addr, migration_tasks[0].task_id)
        check_task_status(status, ["created", "running"], type, "tablet", False)

        await manager.api.disable_injection(servers[0].ip_addr, injection)

    replicas = await get_all_tablet_replicas(manager, servers[0], 'test', 'test')
    assert len(replicas) == 1 and len(replicas[0].replicas) == 1

    intranode_migration_src = replicas[0].replicas[0]
    intranode_migration_dst = (intranode_migration_src[0], 1 - intranode_migration_src[1])
    await asyncio.gather(move_tablet(intranode_migration_src, intranode_migration_dst), check("intranode_migration"))

    migration_src = intranode_migration_dst
    assert migration_src[0] != host_ids[1]
    migration_dst = (host_ids[1], 0)
    await asyncio.gather(move_tablet(migration_src, migration_dst), check("migration"))

@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_tablet_migration_task_list(manager: ManagerClient):
    module_name = "tablets"
    tm = TaskManagerClient(manager.api)
    servers, host_ids = await prepare_migration_test(manager)
    injection = "handle_tablet_migration_end_migration"

    async def move_tablet(server, old_replica, new_replica):
        await manager.api.move_tablet(server.ip_addr, "test", "test", old_replica[0], old_replica[1], new_replica[0], new_replica[1], 0)

    async def check_migration_task_list(type: str):
        # Wait until migration tasks are created.
        migration_tasks0 = await wait_tasks_created(tm, servers[0], module_name, 1, type)
        migration_tasks1 = await wait_tasks_created(tm, servers[1], module_name, 1, type)

        assert len(migration_tasks0) == len(migration_tasks1), f"Different number of migration virtual tasks on nodes {servers[0].server_id} and {servers[1].server_id}"
        assert len(migration_tasks0) == 1, f"Wrong number of migration virtual tasks"

        task0 = migration_tasks0[0]
        task1 = migration_tasks1[0]
        assert task0.task_id == task1.task_id

        for task in [task0, task1]:
            assert task.state in ["created", "running"]
            assert task.type == type
            assert task.kind == "cluster"
            assert task.scope == "tablet"
            assert task.table == "test"
            assert task.keyspace == "test"

        await disable_injection(manager, servers, injection)

    replicas = await get_all_tablet_replicas(manager, servers[0], 'test', 'test')
    assert len(replicas) == 1 and len(replicas[0].replicas) == 1

    intranode_migration_src = replicas[0].replicas[0]
    intranode_migration_dst = (intranode_migration_src[0], 1 - intranode_migration_src[1])
    await enable_injection(manager, servers, injection)
    await asyncio.gather(move_tablet(servers[0], intranode_migration_src, intranode_migration_dst), check_migration_task_list("intranode_migration"))

    migration_src = intranode_migration_dst
    assert migration_src[0] != host_ids[1]
    migration_dst = (host_ids[1], 0)
    await enable_injection(manager, servers, injection)
    await asyncio.gather(move_tablet(servers[0], migration_src, migration_dst), check_migration_task_list("migration"))

@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_tablet_migration_task_failed(manager: ManagerClient):
    module_name = "tablets"
    tm = TaskManagerClient(manager.api)
    servers, host_ids = await prepare_migration_test(manager)

    wait_injection = "stream_tablet_wait"
    throw_injection = "stream_tablet_move_to_cleanup"

    async def move_tablet(old_replica, new_replica):
        await manager.api.move_tablet(servers[0].ip_addr, "test", "test", old_replica[0], old_replica[1], new_replica[0], new_replica[1], 0)

    async def wait_for_task(task_id, type):
        status = await tm.wait_for_task(servers[0].ip_addr, task_id)
        check_task_status(status, ["failed"], type, "tablet", False)

    async def resume_migration(log, mark):
        await log.wait_for('tablet_virtual_task: wait until tablet operation is finished', from_mark=mark)
        await disable_injection(manager, servers, wait_injection)

    async def check(type, log, mark):
        # Wait until migration task is created.
        migration_tasks = await wait_tasks_created(tm, servers[0], module_name, 1, type)
        assert len(migration_tasks) == 1

        await asyncio.gather(wait_for_task(migration_tasks[0].task_id, type), resume_migration(log, mark))

    await enable_injection(manager, servers, wait_injection)
    await enable_injection(manager, servers, throw_injection)

    log = await manager.server_open_log(servers[0].server_id)
    mark = await log.mark()

    replicas = await get_all_tablet_replicas(manager, servers[0], 'test', 'test')
    assert len(replicas) == 1 and len(replicas[0].replicas) == 1

    src = replicas[0].replicas[0]
    dst = (src[0], 1 - src[1])
    await asyncio.gather(move_tablet(src, dst), check("intranode_migration", log, mark))