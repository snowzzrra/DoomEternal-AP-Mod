"""Spawn isolated room map jobs and merge their results in map order."""

from __future__ import annotations

import multiprocessing
import os
import sys
import tempfile
from pathlib import Path

_CONTEXT = None


def worker_count():
    return min(4, max(1, (os.cpu_count() or 1) - 1))


def _initialize(resources, shared):
    global _CONTEXT
    _CONTEXT = {"resources": resources, "shared": shared}


def _compiler(context):
    from .launcher_core import RoomCompiler

    if "compiler" not in context:
        context["compiler"] = RoomCompiler._from_map_worker_resources(*context["resources"])
    return context["compiler"]


def _enemy_map(task, context):
    from tools.maps.ap_map_generator import find_entity_block_bounds
    from tools.maps.mission_complete_map_patcher import patch_mission_complete_maps
    from tools.maps.native_recipe_materializer import materialize_native_recipe_calls
    from tools.release.room_payloads import read_source_snapshots

    from .launcher_core import ROOT, ModCompiler

    compiler = _compiler(context)
    key, row = task["key"], task["row"]
    shared = context["shared"]
    required = compiler._enemy_required_sources({key: row})
    sources = read_source_snapshots(compiler.payload_resource, compiler.payload_manifest, required)
    assets, roots = compiler._enemy_resource_assets({key: row}, shared["precache"], sources, find_entity_block_bounds)
    diagnostics = []
    materialized = materialize_native_recipe_calls(sources[key], key, shared["plan"], sources,
                                                   enemy_revision=9, diagnostics=diagnostics)
    with tempfile.TemporaryDirectory(prefix="map-", dir=task["temporary_root"]) as temporary:
        staged = Path(temporary)
        source, output = staged / f"{key}.source", staged / f"{key}.entities"
        source.write_bytes(materialized.encode("utf-8"))
        ModCompiler(ROOT).compile_map(shared["manifest"], source, output, key,
                                     encounter_source=sources[key], write_seed_sidecar=False)
        audit = patch_mission_complete_maps(ROOT / "data/mission_complete_map_contracts.json",
            {key: output}, staged / "publishers", source_maps=sources,
            retained_overrides=shared["retained_overrides"])
        if audit["unrelated_generated_entity_diff_count"]:
            raise ValueError(f"Enemy map publisher patch changed unrelated entities: {key}")
        content = compiler._compress_entities_text(output.read_bytes().decode("utf-8"))
    return content, list(assets[key]), roots, diagnostics


def _placement_map(task, context):
    compiler = _compiler(context)
    shared = context["shared"]
    if "policies" not in context:
        context["policies"] = compiler._placement_entity_policies(
            shared["placements"], shared["options"], None)
    return compiler._apply_placement_entity(task["key"], task["content"], context["policies"],
                                           temporary_root=task["temporary_root"])


def room_map_worker(task, context=None):
    from doom_eap.runtime.observers import SlotAdmissionError
    from tools.maps.native_recipe_materializer import RecipeMaterializationError

    context = _CONTEXT if context is None else context
    try:
        result = _map_task(task, context)
        return task["key"], result, None
    except (RecipeMaterializationError, SlotAdmissionError) as error:
        # these exceptions need both code and message when reconstructed after spawn.
        return task["key"], None, (type(error), error.code, str(error))


def _map_task(task, context):
    with tempfile.TemporaryDirectory(prefix="worker-", dir=task["temporary_root"]) as temporary:
        previous = tempfile.tempdir
        try:
            if multiprocessing.parent_process() is not None:
                tempfile.tempdir = temporary
            task = {**task, "temporary_root": temporary}
            return _enemy_map(task, context) if task["kind"] == "enemy" else _placement_map(task, context)
        finally:
            tempfile.tempdir = previous


def _collect_result(result, results):
    key, value, error = result
    if error is not None:
        error_type, code, message = error
        raise error_type(code, message)
    results[key] = value


def _pool_results(pool, jobs, progress):
    results = {}
    pending = pool.imap_unordered(room_map_worker, jobs, chunksize=1)
    processes = tuple(pool._pool)
    while len(results) < len(jobs):
        if progress is not None:
            progress(len(results), len(jobs))
        try:
            result = pending.next(timeout=0.1)
        except multiprocessing.TimeoutError:
            if any(process.exitcode is not None for process in processes):
                raise ValueError("Room map worker exited before returning its result") from None
            continue
        _collect_result(result, results)
    return results


def _run_pool(jobs, resources, shared, progress):
    context = multiprocessing.get_context("spawn")
    if os.name == "nt" and not getattr(sys, "frozen", False):
        # the base interpreter preserves spawn's pipe across a Windows venv.
        executable = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
        if executable.is_file():
            context.set_executable(executable)
    pool = context.Pool(worker_count(), initializer=_initialize, initargs=(resources, shared))
    try:
        return _pool_results(pool, jobs, progress)
    finally:
        pool.terminate()
        pool.join()


def run_map_workers(jobs, resources, shared, progress=None):
    if not jobs:
        return []
    with tempfile.TemporaryDirectory(prefix="doom-ap-map-workers-") as temporary:
        jobs = [{**job, "temporary_root": temporary} for job in jobs]
        if worker_count() == 1:
            results = {}
            context = {"resources": resources, "shared": shared}
            for job in jobs:
                if progress is not None:
                    progress(len(results), len(jobs))
                _collect_result(room_map_worker(job, context), results)
        else:
            results = _run_pool(jobs, resources, shared, progress)
        if progress is not None:
            progress(len(jobs), len(jobs))
    return sorted(results.items())
