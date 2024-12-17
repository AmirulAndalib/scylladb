/*
 * Copyright (C) 2024-present ScyllaDB
 */

/*
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

#pragma once

#include "schema/schema_fwd.hh"
#include "tasks/task_manager.hh"

namespace locator {
class tablet_id;
enum class tablet_task_type;
class tablet_replica;
}

namespace service {

class storage_service;

class tablet_virtual_task : public tasks::task_manager::virtual_task::impl {
private:
    storage_service& _ss;
private:
    static constexpr auto TABLE_ID = "table_id";
public:
    tablet_virtual_task(tasks::task_manager::module_ptr module,
            service::storage_service& ss)
        : tasks::task_manager::virtual_task::impl(std::move(module))
        , _ss(ss)
    {}
    virtual tasks::task_manager::task_group get_group() const noexcept override;
    virtual future<std::optional<tasks::virtual_task_hint>> contains(tasks::task_id task_id) const override;
    virtual future<tasks::is_abortable> is_abortable(tasks::virtual_task_hint hint) const override;

    virtual future<std::optional<tasks::task_status>> get_status(tasks::task_id id, tasks::virtual_task_hint hint) override;
    virtual future<std::optional<tasks::task_status>> wait(tasks::task_id id, tasks::virtual_task_hint hint) override;
    virtual future<> abort(tasks::task_id id, tasks::virtual_task_hint hint) noexcept override;
    virtual future<std::vector<tasks::task_stats>> get_stats() override;
private:
    std::vector<table_id> get_table_ids() const;
    future<std::optional<tasks::task_status>> get_status_helper(tasks::task_id id, utils::chunked_vector<locator::tablet_id>& tablets, tasks::virtual_task_hint hint, std::optional<locator::tablet_replica>& pending_replica);
};

class task_manager_module : public tasks::task_manager::module {
public:
    task_manager_module(tasks::task_manager& tm) noexcept;
};
}