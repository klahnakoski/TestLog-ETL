# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import division
from __future__ import unicode_literals

from future.utils import text_type
from collections import Mapping

import requests
from jx_python import jx

from mo_logs import Log, machine_metadata, strings

from mo_hg.hg_mozilla_org import minimize_repo
from mo_logs.exceptions import suppress_exception, Except
from activedata_etl import etl2key
from activedata_etl.imports.resource_usage import normalize_resource_usage
from activedata_etl.imports.text_log import process_tc_live_log
from activedata_etl.transforms import TRY_AGAIN_LATER
from mo_dots import set_default, Data, unwraplist, listwrap, wrap, coalesce
from pyLibrary import convert
from pyLibrary.env import http
from mo_math import Math
from mo_testing.fuzzytestcase import assertAlmostEqual
from mo_times.dates import Date

DEBUG = True
DISABLE_LOG_PARSING = False
MAX_THREADS = 5
MAIN_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}"
STATUS_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}/status"
ARTIFACTS_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}/artifacts"
ARTIFACT_URL = "http://queue.taskcluster.net/v1/task/{{task_id}}/artifacts/{{path}}"

RETRY = {"times": 3, "sleep": 5}
seen_tasks = {}
new_seen_tc_properties = set()


def process(source_key, source, destination, resources, please_stop=None):
    output = []
    source_etl = None

    lines = list(enumerate(source.read_lines()))
    session = requests.session()
    for line_number, line in lines:
        if please_stop:
            Log.error("Shutdown detected. Stopping early")
        try:
            tc_message = convert.json2value(line)
            task_id = consume(tc_message, "status.taskId")
            etl = consume(tc_message, "etl")
            consume(tc_message, "_meta")

            Log.note("{{id}} found (line #{{num}})", id=task_id, num=line_number, artifact=tc_message.artifact.name)
            task_url = strings.expand_template(MAIN_URL, {"task_id": task_id})
            task = http.get_json(task_url, retry=RETRY, session=session)
            if task.code == u'ResourceNotFound':
                Log.note("Can not find task {{task}} while processing key {{key}}", key=source_key, task=task_id)
                if not source_etl:
                    # USE ONE SOURCE ETL, OTHERWISE WE MAKE TOO MANY KEYS
                    source_etl = etl
                    if not source_etl.source.source:  # FIX ONCE TC LOGGER IS USING "tc" PREFIX FOR KEYS
                        source_etl.source.type = "join"
                        source_etl.source.source = {"id": "tc"}

                normalized = Data(
                    task={"id": task_id},
                    etl={
                        "id": line_number,
                        "source": source_etl,
                        "type": "join",
                        "timestamp": Date.now(),
                        "error": "not found",
                        "machine": machine_metadata
                    }
                )

                output.append(normalized)

                continue

            # if not tc_message.status.runs.last().resolved:
            # UPDATE TASK STATUS (tc_message MAY BE OLD)
            status_url = strings.expand_template(STATUS_URL, {"task_id": task_id})
            task_status = http.get_json(status_url, retry=RETRY, session=session)
            consume(task_status, "status.taskId")
            temp_runs, task_status.status.runs = task_status.status.runs, None  # set_default() will screw `runs` up
            set_default(tc_message.status, task_status.status)
            tc_message.status.runs = [set_default(r, tc_message.status.runs[ii]) for ii, r in enumerate(temp_runs)]
            if not tc_message.status.runs.last().resolved:
                Log.error(TRY_AGAIN_LATER, reason="task still runnning (not \"resolved\")")

            normalized = _normalize(source_key, task_id, tc_message, task, resources)

            # get the artifact list for the taskId
            try:
                artifacts = normalized.task.artifacts = http.get_json(strings.expand_template(ARTIFACTS_URL, {"task_id": task_id}), retry=RETRY).artifacts
            except Exception as e:
                Log.error(TRY_AGAIN_LATER, reason="Can not get artifacts for task " + task_id, cause=e)

            for a in artifacts:
                a.url = strings.expand_template(ARTIFACT_URL, {"task_id": task_id, "path": a.name})
                a.expires = Date(a.expires)
                if a.name.endswith("/live.log"):
                    try:
                        read_actions(source_key, normalized, a.url)
                    except Exception as e:
                        if "could not connect" in e and normalized.task.run.status != "completed":  # in ["deadline-exceeded"]:
                            # THIS IS EXPECTED WHEN THE TASK IS IN AN ERROR STATE, CHECK IT AND IGNORE
                            pass
                        elif TRY_AGAIN_LATER in e:
                            Log.error("Aborting processing of {{url}} for key={{key}}", url=a.url, key=source_key, cause=e)
                        else:
                            # THIS IS EXPECTED WHEN THE TASK IS IN AN ERROR STATE, CHECK IT AND IGNORE
                            Log.warning("Problem reading artifact {{url}} for key={{key}}", url=a.url, key=source_key, cause=e)
                elif a.name.endswith("/resource-usage.json"):
                    with suppress_exception:
                        normalized.resource_usage = normalize_resource_usage(a.url)
            # FIX THE ETL
            if not source_etl:
                # USE ONE SOURCE ETL, OTHERWISE WE MAKE TOO MANY KEYS
                source_etl = etl
                if not source_etl.source.source:  # FIX ONCE TC LOGGER IS USING "tc" PREFIX FOR KEYS
                    source_etl.source.type = "join"
                    source_etl.source.source = {"id": "tc"}
            normalized.etl = {
                "id": line_number,
                "source": source_etl,
                "type": "join",
                "timestamp": Date.now(),
                "machine": machine_metadata
            }

            tc_message.artifact = "." if tc_message.artifact else None
            if normalized.task.id in seen_tasks:
                try:
                    assertAlmostEqual([tc_message, task, artifacts], seen_tasks[normalized.task.id], places=11)
                except Exception as e:
                    Log.error("Not expected", cause=e)
            else:
                tc_message._meta = None
                tc_message.runs = None
                tc_message.runId = None
                tc_message.artifact = None
                seen_tasks[normalized.task.id] = [tc_message, task, artifacts]

            output.append(normalized)
        except Exception as e:
            if TRY_AGAIN_LATER in e:
                raise e
            Log.warning("TaskCluster line not processed for key {{key}}: {{line|quote}}", key=source_key, line=line, cause=e)

    keys = destination.extend({"id": etl2key(t.etl), "value": t} for t in output)
    return keys


def read_actions(source_key, normalized, url):
    if DISABLE_LOG_PARSING:
        return
    try:
        all_log_lines = http.get(url).get_all_lines(encoding=None)
        normalized.action = process_tc_live_log(all_log_lines, url, normalized)
    except Exception as e:
        e = Except.wrap(e)
        if "Read timed out" in e:
            Log.error(TRY_AGAIN_LATER, reason="read timeout")
        elif "Failed to establish a new connection" in e:
            Log.error(TRY_AGAIN_LATER, reason="could not connect")
        elif "An existing connection was forcibly closed by the remote host" in e:
            Log.error(TRY_AGAIN_LATER, reason="text log was forcibly closed")
        else:
            Log.error("problem processing {{key}}", key=source_key, cause=e)


def _normalize(source_key, task_id, tc_message, task, resources):
    output = Data()
    set_default(task, consume(tc_message, "status"))

    output.task.id = task_id
    output.task.created = Date(consume(task, "created"))
    output.task.deadline = Date(consume(task, "deadline"))
    output.task.dependencies = unwraplist(consume(task, "dependencies"))
    output.task.expires = Date(consume(task, "expires"))
    output.task.maxRunTime = consume(task, "payload.maxRunTime")
    if output.task.maxRunTime == "hello":
        output.task.maxRunTime = None

    env = consume(task, "payload.env")
    output.task.env = _object_to_array(env, "name", "value")

    features = consume(task, "payload.features")
    if all(isinstance(v, bool) for v in features.values()):
        output.task.features = [k if v else "!" + k for k, v in features.items()]
    else:
        Log.error("Unexpected features: {{features|json}}", features=features)
    output.task.cache = _object_to_array(consume(task, "payload.cache"), "name", "value")
    output.task.requires = consume(task, "requires")
    output.task.capabilities = consume(task, "payload.capabilities")

    image = consume(task, "payload.image")
    if isinstance(image, basestring):
        output.task.image = {"path": image}
    else:
        output.task.image = image

    output.task.priority = consume(task, "priority")
    output.task.provisioner.id = consume(task, "provisionerId")
    output.task.retries.remaining = consume(task, "retriesLeft")
    output.task.retries.total = consume(task, "retries")
    output.task.routes = consume(task, "routes")

    run_id = coalesce(consume(tc_message, "runId"), len(task.runs) - 1)
    output.task.run = _normalize_task_run(task.runs[run_id])
    output.task.runs = map(_normalize_task_run, consume(task, "runs"))
    output.task.reboot = consume(task, "payload.reboot")

    output.task.scheduler.id = consume(task, "schedulerId")
    output.task.scopes = consume(task, "scopes")
    output.task.state = consume(task, "state")
    output.task.group.id = consume(task, "taskGroupId")
    output.task.version = consume(tc_message, "version")
    output.task.worker.group = consume(tc_message, "workerGroup")
    output.task.worker.id = consume(tc_message, "workerId")
    output.task.worker.type = consume(task, "workerType")

    output.task.manifest.task_id = consume(task, "payload.taskid_of_manifest")
    output.task.manifest.update = consume(task, "payload.update_manifest")
    output.task.beetmove.task_id = coalesce_w_conflict_detection(
        source_key,
        consume(task, "payload.taskid_to_beetmove"),
        consume(task, "payload.properties.taskid_to_beetmove")
    )

    # DELETE JUNK
    consume(task, "payload.routes")
    consume(task, "payload.log")
    consume(task, "payload.upstreamArtifacts")
    output.task.signing.cert = coalesce(*listwrap(consume(task, "payload.signing_cert"))),  # OFTEN HAS NULLS
    output.task.parent.id = coalesce_w_conflict_detection(
        source_key,
        consume(task, "parent_task_id"),
        consume(task, "payload.properties.parent_task_id")
    )
    output.task.parent.artifacts_url = consume(task, "payload.parent_task_artifacts_url")


    # MOUNTS
    output.task.mounts = consume(task, "payload.mounts")

    artifacts = consume(task, "payload.artifacts")
    try:

        if isinstance(artifacts, list):
            for a in artifacts:
                if not a.name:
                    if not a.path:
                        Log.error("expecting name, or path of artifact")
                    else:
                        a.name = a.path
            output.task.artifacts = artifacts
        else:
            output.task.artifacts = unwraplist(_object_to_array(artifacts, "name"))
    except Exception as e:
        Log.warning("artifact format problem in {{key}}:\n{{artifact|json|indent}}", key=source_key, artifact=task.payload.artifacts, cause=e)
    output.task.cache = unwraplist(_object_to_array(task.payload.cache, "name", "path"))
    try:
        command = consume(task, "payload.command")
        cmd = consume(task, "payload.cmd")
        command = [cc for c in (command if command else cmd) for cc in listwrap(c)]   # SOMETIMES A LIST OF LISTS
        output.task.command = " ".join(map(convert.string2quote, map(text_type.strip, command)))
    except Exception as e:
        Log.error("problem", cause=e)

    set_build_info(source_key, output, task, env, resources)
    _normalize_run(source_key, output, task, env)

    output.task.tags = get_tags(source_key, output.task.id, task)

    output.build.type = unwraplist(list(set(listwrap(output.build.type))))

    # PROPERTIES THAT HAVE NOT BEEN HANDLED
    remaining_keys = set([k for k, v in task.leaves()] + [k for k, v in tc_message.leaves()]) - new_seen_tc_properties
    if remaining_keys:
        map(new_seen_tc_properties.add, remaining_keys)
        Log.warning("Some properties ({{props|json}}) are not consumed while processing key {{key}}", key=source_key, props=remaining_keys)

    return output


def _normalize_task_run(run):
    output = Data()
    output.reason_created = run.reasonCreated
    output.id = run.id
    output.scheduled = Date(run.scheduled)
    output.start_time = Date(run.started)
    output.status = run.reasonResolved
    output.end_time = Date(run.resolved)
    output.duration = Date(run.resolved) - Date(run.started)
    output.state = run.state
    output.worker.group = run.workerGroup
    output.worker.id = run.workerId
    return output


def _normalize_run(source_key, normalized, task, env):
    """
    Get the run object that contains properties that describe the run of this job
    :param task: The task definition
    :return: The run object
    """

    run_type = []
    metadata_name = consume(task, "metadata.name")
    if "-e10s" in metadata_name:
        metadata_name = metadata_name.replace("-e10s", "")
        run_type += ["e10s"]
    elif "e10s" in metadata_name:
        Log.error("not expected")

    # PARSE TEST SUITE NAME
    suite = consume(task, "extra.suite")
    test = suite.name.lower()

    # FLAVOR
    flavor = suite.flavor.lower()
    if test == flavor:
        flavor = None
    elif flavor.startswith(test + "-"):
        flavor = flavor[len(test) + 1::]

    if test.startswith("mochitest-"):
        # mochitest-chrome
        # mochitest-media-2
        # mochitest-plain-clipboard
        path = test.split("-")
        test = path[0]
        flavor = "-".join(path[:-1]) + ("-" + flavor if flavor else "")

    if flavor and "-e10s" in flavor:
        flavor = flavor.replace("-e10s", "").strip()
        if not flavor:
            flavor = None
        run_type += ["e10s"]

    if flavor=="chunked":
        flavor = None
        run_type += ["chunked"]
    elif flavor and "-chunked" in flavor:
        flavor = flavor.replace("-chunked", "").strip()
        if not flavor:
            flavor = None
        run_type += ["chunked"]

    # CHUNK NUMBER
    chunk = None
    path = test.split("-")
    if Math.is_integer(path[-1]):
        chunk = int(path[-1])
        test = "-".join(path[:-1])
    chunk = coalesce_w_conflict_detection(
        source_key,
        consume(task, "extra.chunks.current"),
        consume(task, "payload.properties.THIS_CHUNK"),
        chunk
    )
    test = coalesce_w_conflict_detection(
        source_key,
        test,
        consume(task, "tags.test-type")
    )

    if test == None:
        fullname = None
    elif flavor == None:
        fullname = test
    else:
        fullname = test + "-" + flavor

    set_default(
        normalized,
        {"run": {
            "key": consume(task, "payload.buildername"),
            "name": metadata_name,
            "machine": normalized.treeherder.machine,
            "suite": {"name": test, "flavor": flavor, "fullname": fullname},
            "chunk": chunk,
            "type": unwraplist(list(set(run_type))),
            "timestamp": normalized.task.run.start_time
        }}
    )


def set_build_info(source_key, normalized, task, env, resources):
    """
    Get a build object that describes the build
    :param task: The task definition
    :return: The build object
    """

    if task.workerType.startswith("dummy-type"):
        task.workerType = "dummy-type"

    build_type = consume(task, "extra.build_type")

    build_product = coalesce_w_conflict_detection(
        source_key,
        consume(task, "payload.properties.product"),
        consume(task, "tags.build_props.product"),
        task.extra.treeherder.productName,
        consume(task, "extra.build_product"),
        "firefox" if task.extra.suite.name.startswith("firefox") else None,
        "firefox" if any(r.startswith("index.gecko.v2.try.latest.firefox.") for r in normalized.task.routes) else None
    )

    set_default(
        normalized,
        {"build": {
            "name": consume(task, "extra.build_name"),
            "product": build_product,
            "platform": coalesce_w_conflict_detection(
                source_key,
                task.extra.treeherder.build.platform,
                task.extra.treeherder.machine.platform
            ),
            # MOZILLA_BUILD_URL looks like this:
            # "https://queue.taskcluster.net/v1/task/e6TfNRfiR3W7ZbGS6SRGWg/artifacts/public/build/target.tar.bz2"
            "url": env.MOZILLA_BUILD_URL,
            "revision": coalesce_w_conflict_detection(
                source_key,
                consume(task, "tags.build_props.revision"),
                consume(task, "payload.sourcestamp.revision"),
                consume(task, "payload.properties.revision"),
                env.GECKO_HEAD_REV
            ),
            "type": listwrap({"dbg": "debug"}.get(build_type, build_type)),
            "version": consume(task, "tags.build_props.version"),
            "channel": consume(task, "payload.properties.channels")
        }}
    )

    if normalized.build.platform.endswith("-ccov"):
        normalized.build.platform = normalized.build.platform.split("-")[0]
        normalized.build.type += ["ccov"]
    if normalized.build.platform.endswith("-jsdcov"):
        normalized.build.platform = normalized.build.platform.split("-")[0]
        normalized.build.type += ["jsdcov"]

    normalized.build.branch = coalesce_w_conflict_detection(
        source_key,
        consume(task, "tags.build_props.branch"),
        consume(task, "payload.sourcestamp.branch").split("/")[-1],
        env.GECKO_HEAD_REPOSITORY.strip("/").split("/")[-1],   # will look like "https://hg.mozilla.org/try/"
        consume(task, "payload.properties.repo_path").split("/")[-1],
        env.MH_BRANCH
    )
    normalized.build.revision12 = normalized.build.revision[0:12]

    if normalized.build.revision:
        normalized.repo = resources.hg.get_revision(wrap({"branch": {"name": normalized.build.branch}, "changeset": {"id": normalized.build.revision}}))
        minimize_repo(normalized.repo)
        normalized.build.date = normalized.repo.push.date

    treeherder = consume(task, "extra.treeherder")
    if treeherder:
        for l, v in treeherder.leaves():
            normalized.treeherder[l] = v

    normalized.task.kind = consume(task, "tags.kind")

    for k, v in BUILD_TYPES.items():
        if treeherder.collection[k]:
            normalized.build.type += v

    diff = treeherder.collection.keys() - BUILD_TYPE_KEYS
    if diff:
        Log.warning("new collection type of {{type}} while processing key {{key}}", type=diff, key=source_key)

    # FIND BUILD TASK
    if treeherder.jobKind == 'test':
        build_task = get_build_task(source_key, resources, normalized)
        if build_task:
            Log.note("Got build {{build}} for test {{test}}", build=build_task.task.id, test=normalized.task.id)
            minimize_repo(build_task.repo)

            build_task._id = None
            build_task.task.artifacts = None
            build_task.task.command = None
            build_task.task.env = None
            build_task.task.scopes = None
            build_task.task.runs = None
            build_task.task.routes = None
            build_task.task.tags = None
            build_task.action.timings = None
            build_task.etl = None
            set_default(normalized.build, build_task)


MISSING_BUILDS = set()


def get_build_task(source_key, resources, normalized_task):
    # "revision12":"571286200177",
    # "url":"https://queue.taskcluster.net/v1/task/J4jnKgKAQieAhwvSQBKa3Q/artifacts/public/build/target.tar.bz2",
    # "platform":"linux64",
    # "branch":"graphics",
    # "date":1484242475,
    # "type":"opt",
    # "revision":"571286200177ae7ddfa1893c6b42853b60f2e81e"

    build_task_id = listwrap(coalesce(strings.between(normalized_task.build.url, "task/", "/"), normalized_task.task.dependencies))
    if not build_task_id:
        Log.warning("Could not find build.url {{task}} in {{key}}", task=normalized_task.task.id, key=source_key)
        return None
    response = http.post_json(
        resources.local_es_node + "/task/task/_search",
        data={
            "query": {"filtered": {"filter": {"terms": {
                "task.id": build_task_id
            }}}},
            "from": 0,
            "size": 10
        },
        retry={"times": 3, "sleep": 15}
    )

    candidates = jx.sort(
        [
            h._source
            for h in response.hits.hits
            if h._source.treeherder.jobKind == "build"
        ],
        "run.start_time"
    )
    if not candidates:
        if not any(b in MISSING_BUILDS for b in build_task_id):
            Log.alert(
                "Could not find any build task {{build}} for test {{task}} in {{key}}",
                task=normalized_task.task.id,
                build=build_task_id,
                key=source_key
            )
            MISSING_BUILDS.update(build_task_id)
        return None

    candidate = candidates.last()
    # if len(candidates) > 1:
    #     etl, candidate.etl = candidate.etl, None
    #     _id, candidate._id = candidate._id, None
    #     for c in candidates.not_right(1):
    #         try:
    #             assertAlmostEqual(c, candidate)
    #         except Exception as e:
    #             Log.warning(
    #                 "Found too many builds ({{num}}) with task id={{task}} in {{key}}, choosing last",
    #                 task=build_task_id,
    #                 key=source_key,
    #                 num=len(candidates),
    #                 cause=e
    #             )
    #     candidate.etl = etl
    #     candidate._id = _id

    if normalized_task.build.revision12 != None and candidate.build.revision12 != normalized_task.build.revision12:
        Log.warning(
            "Could not find matching build task {{build}} for test {{task}} in {{key}}",
            task=normalized_task.task.id,
            build=build_task_id,
            key=source_key
        )
        return None

    return candidate


def get_tags(source_key, task_id, task, parent=None):
    tags = []
    # SPECIAL CASES
    platforms = consume(task, "payload.properties.platforms")
    if isinstance(platforms, text_type):
        platforms = map(text_type.strip, platforms.split(","))
        tags.append({"name": "platforms", "value": platforms})
    link = consume(task, "payload.link")
    if link:
        tags.append({"name": "link", "value": link})

    # VARIOUS LOCATIONS TO FIND TAGS
    t = consume(task, "tags").leaves()
    m = consume(task, "metadata").leaves()
    e = consume(task, "extra").leaves()
    p = consume(task, "payload.properties").leaves()
    g = [(k, consume(task.payload, k)) for k in PAYLOAD_PROPERTIES]

    tags.extend({"name": k, "value": v} for k, v in t)
    tags.extend({"name": k, "value": v} for k, v in m)
    tags.extend({"name": k, "value": v} for k, v in e)
    tags.extend({"name": k, "value": v} for k, v in p)
    tags.extend({"name": k, "value": v} for k, v in g)

    clean_tags = []
    for t in tags:
        # ENSURE THE VALUES ARE UNICODE
        if parent:
            t['name'] = parent + "." + t['name']
        v = t["value"]
        if v == None:
            continue
        elif isinstance(v, list):
            if len(v) == 1:
                v = v[0]
                if isinstance(v, Mapping):
                    for tt in get_tags(source_key, task_id, Data(tags=v), parent=t['name']):
                        clean_tags.append(tt)
                    continue
                elif not isinstance(v, text_type):
                    v = convert.value2json(v)
            # elif all(isinstance(vv, (text_type, float, int)) for vv in v):
            #     pass  # LIST OF PRIMITIVES IS OK
            else:
                v = convert.value2json(v)
        elif not isinstance(v, text_type):
            v = convert.value2json(v)
        t["value"] = v
        verify_tag(source_key, task_id, t)
        clean_tags.append(t)

    return clean_tags


def verify_tag(source_key, task_id, t):
    if not isinstance(t["value"], text_type):
        Log.error("Expecting unicode")
    if t["name"] not in KNOWN_TAGS:
        Log.warning("unknown task tag {{tag|quote}} while processing {{task_id}} in {{key}}", key=source_key, id=task_id, tag=t["name"])
        KNOWN_TAGS.add(t["name"])


def coalesce_w_conflict_detection(source_key, *args):
    if len(args)<2:
        Log.error("bad call to coalesce, expecting source_key as first parameter")
    output = None
    for a in args:
        if a == None:
            continue
        if output == None:
            output = a
        elif a != output:
            Log.warning("tried to coalesce {{values_|json}} while processing {{key}}", key=source_key, values_=args)
        else:
            pass
    return output


def _scrub(record, name):
    value = record[name]
    record[name] = None
    if value == "-" or value == "":
        return None
    else:
        return unwraplist(value)


def _object_to_array(value, key_name, value_name=None):
    try:
        if value_name==None:
            return unwraplist([set_default(v, {key_name: k}) for k, v in value.items()])
        else:
            return unwraplist([{key_name: k, value_name: v} for k, v in value.items()])
    except Exception as e:
        Log.error("unexpected", cause=e)


BUILD_TYPES = {
    "arm-debug": ["debug", "arm"],
    "arm-opt": ["opt", "arm"],
    "asan": ["asan"],
    "ccov": ["ccov"],
    "debug": ["debug"],
    "fuzz": ["fuzz"],
    "gyp": ["gyp"],
    "gyp-asan": ["gyp", "asan"],
    "jsdcov": ["jsdcov"],
    "lsan": ["lsan"],
    "make": ["make"],
    "memleak": ["memleak"],
    "opt": ["opt"],
    "pgo": ["pgo"],
    "nostylo": ["nostylo"],
    "ubsan": ["ubsan"]
}
BUILD_TYPE_KEYS = set(BUILD_TYPES.keys())

PAYLOAD_PROPERTIES = {
    "apks.armv7_v15",
    "apks.x86",
    "artifactsTaskId",
    "balrog_api_root",
    "build_number",
    "chain",
    "CHANNEL",
    "contact",
    "context",
    "created",
    "deadline",
    "description",
    "desiredResolution",
    "encryptedEnv",
    "en_us_binary_url",
    "google_play_track",
    "graphs",  # POINTER TO graph.json ARTIFACT
    "locales",
    "locale",
    "mar_tools_url",
    "next_version",
    "NO_BBCONFIG",
    "onExitStatus",
    "osGroups",
    "purpose",
    "release_promotion",
    "repack_manifests_url",
    "script_repo_revision",
    "signingManifest",
    "sourcestamp.repository",
    "stage-product",
    "summary",
    "supersederUrl",
    "template_key",
    "THIS_CHUNK",
    "TOTAL_CHUNKS",
    "tuxedo_server_url",
    "unsignedArtifacts",
    "upload_date",
    "VERIFY_CONFIG",
    "version"

}

KNOWN_TAGS = {
    "buildid",
    "build_name",
    "build_type",
    "build_product",
    "build_props.branch",
    "build_props.build_number",
    "build_props.release_eta",
    "build_props.locales",
    "build_props.mozharness_changeset",
    "build_props.partials",
    "build_props.platform",
    "build_props.product",
    "build_props.revision",
    "build_props.version",

    "chainOfTrust.inputs.docker-image",


    "chunks.current",
    "chunks.total",
    "crater.crateName",
    "crater.toolchain.customSha",
    "crater.crateVers",
    "crater.taskType",
    "crater.toolchain.archiveDate",
    "crater.toolchain.channel",
    "crater.toolchainGitRepo",
    "crater.toolchainGitSha",

    "createdForUser",
    "data.base.sha",
    "data.base.user.login",
    "data.head.sha",
    "data.head.user.email",
    "description",

    "en_us_installer_binary_url",

    "funsize.partials",
    "funsize.partials.branch",
    "funsize.partials.from_mar",
    "funsize.partials.locale",
    "funsize.partials.platform",
    "funsize.partials.previousBuildNumber",
    "funsize.partials.previousVersion",
    "funsize.partials.to_mar",
    "funsize.partials.toBuildNumber",
    "funsize.partials.toVersion",
    "funsize.partials.update_number",

    "github.branches",
    "github.events",
    "github.env",
    "github.headBranch",
    "github.headRepo",
    "github.headRevision",
    "github.headUser",
    "github.baseBranch",
    "github.baseRepo",
    "github.baseRevision",
    "github.baseUser",
    "githubPullRequest",

    "imageMeta.contextHash",
    "imageMeta.imageName",
    "imageMeta.level",
    "index.data.hello",
    "index.expires",
    "index.rank",
    "installer_path",
    "l10n_changesets",

    "link",
    "locations.mozharness",
    "locations.test_packages",
    "locations.build",
    "locations.img",
    "locations.mar",
    "locations.sources",
    "locations.symbols",
    "locations.tests",
    "name",

    "notification.task-defined.irc.notify_nicks",
    "notification.task-defined.irc.message",
    "notification.task-defined.log_collect",
    "notification.task-defined.ses.body",
    "notification.task-defined.ses.recipients",
    "notification.task-defined.ses.subject",
    "notification.task-defined.smtp.body",
    "notification.task-defined.smtp.recipients",
    "notification.task-defined.smtp.subject",
    "notification.task-defined.sns.message",
    "notification.task-defined.sns.arn",

    "notifications.task-completed.message",
    "notifications.task-completed.ids",
    "notifications.task-completed.subject",
    "notifications.task-failed.message",
    "notifications.task-failed.ids",
    "notifications.task-failed.subject",
    "notifications.task-exception.message",
    "notifications.task-exception.ids",
    "notifications.task-exception.subject",

    "npmCache.url",
    "npmCache.expires",
    "objective",
    "owner",
    "partial_versions",
    "platforms",
    "signing.signature",
    "source",
    "suite.flavor",
    "suite.name",

    "treeherderEnv",

    "upload_to_task_id",
    "url.busybox",
    "useCloudMirror",
    "who"
} | PAYLOAD_PROPERTIES

def consume(props, key):
    output, props[key] = props[key], None
    return output
