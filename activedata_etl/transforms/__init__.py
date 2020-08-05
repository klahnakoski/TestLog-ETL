# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Contact: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import division
from __future__ import unicode_literals

from mo_json import null
from mo_logs import Log

from mo_dots import Data, set_default, Null
from mo_future import text
from mo_http import http
from mo_times.dates import Date
from pyLibrary.env import git

DEBUG = False
DEBUG_SHOW_LINE = True
DEBUG_SHOW_NO_LOG = False
TOO_MANY_FAILS = 5  # STOP LOOKING AT AN ARTIFACT AFTER THIS MANY WITH NON-JSON LINES

ACTIVE_DATA_QUERY = "https://activedata.allizom.org/query"

TC_MAIN_URL = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/{{task_id}}"
TC_STATUS_URL = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/{{task_id}}/status"
TC_ARTIFACTS_URL = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/{{task_id}}/artifacts"
TC_ARTIFACT_URL = "https://firefox-ci-tc.services.mozilla.com/api/queue/v1/task/{{task_id}}/artifacts/{{path}}"
TC_RETRY = {"times": 3, "sleep": 5}

TRY_AGAIN_LATER = "{{reason}}, try again later"

STRUCTURED_LOG_ENDINGS = [
    "structured_logs.log",
    "_structured_full.log",
    '_raw.log',
    '.jsonl'
]




NOT_STRUCTURED_LOGS = [
    ".apk",
    "/awsy_raw.log",
    "/buildbot_properties.json",
    "/buildprops.json",
    "/chain_of_trust.log",
    "/chainOfTrust.json.asc",
    ".checksums.asc",
    ".checksums",
    "/talos_raw.log",
    ".mozinfo.json",
    "_errorsummary.log",
    ".exe",
    ".extra",
    ".dmg",
    ".dmp",
    "-grcov.zip",
    "-jsvm.zip",
    ".langpack.xpi",
    "/live.log",
    "/live_backing.log",
    "/log_critical.log",
    "/log_error.log",
    "/log_fatal.log",
    "/log_info.log",
    "/log_warning.log",
    "/log_raw.log",
    "/localconfig.json",
    ".mar",
    "/master.tar.gz",
    "/mar.exe",
    "/manifest.json",
    "/mbsdiff.exe",
    "/mitmproxy.log",
    "/mozharness.zip",
    "partner_repack_raw.log",
    "perfherder-data.json",
    ".png",
    "/properties.json",
    "/raptor_raw.log",
    "/single_locale_raw.log",
    "/talos_critical.log",
    "/talos_error.log",
    "/talos_fatal.log",
    "/talos_info.log",
    "/talos_warning.log",
    ".tests.tar.gz",
    ".tests.zip",
    "/tests-by-manifest.json.gz",
    "/.tar.gz",
    ".test_packages.json",
    "/xvfb.log",
    "/xsession-errors.log",
    "resource-usage.json",
    ".html",
    ".pom.sha1",
    ".pom",
    ".xml.sha1",
    ".xml",
    ]
TOO_MANY_NON_JSON_LINES = Data()

next_key = {}  # TRACK THE NEXT KEY FOR EACH SOURCE KEY


class Transform(object):

    def __call__(self, source_key, source, destination, resources, please_stop=None):
        """
        :param source_key: THE DOT-DELIMITED PATH FOR THE SOURCE
        :param source: A LINE GENERATOR WITH ETL ARTIFACTS (LIKELY JSON)
        :param destination: THE s3 BUCKET TO PLACE ALL THE TRANSFORM RESULTS
        :param resources: VARIOUS EXTRA RESOURCES TO HELP WITH ANNOTATING THE DATA
        :param please_stop: CHECK REGULARLY, AND EXIT TRANSFORMATION IF True
        :return: list OF NEW KEYS, WITH source_key AS THEIR PREFIX
        """
        raise NotImplementedError


def get_test_result_content(line_number, name, url):
    """
    :param line_number:  for debugging
    :param name:  for debugging
    :param url:  TO BE READ
    :return:  RETURNS BYTES **NOT** UNICODE
    """
    if any(name.endswith(e) for e in STRUCTURED_LOG_ENDINGS):
        # FAST TRACK THE FILES WE SUSPECT TO BE STRUCTURED LOGS ALREADY
        response = http.get(url)
        logs = response.all_lines
        return logs, "unknown"

    return None, 0


class EtlHeadGenerator(object):
    """
    WILL RETURN A UNIQUE ETL STRUCTURE, GIVEN A SOURCE AND A DESTINATION NAME
    """

    def __init__(self, source_key):
        self.source_key = source_key
        self.next_id = 0

    def next(
        self,
        source_etl,  # ETL STRUCTURE DESCRIBING SOURCE
        **kwargs # URL FOR THE DATA
    ):
        num = self.next_id
        self.next_id = num + 1
        dest_key = self.source_key + "." + text(num)

        dest_etl = set_default(
            {
                "id": num,
                "source": source_etl,
                "type": "join",
                "revision": git.get_revision(),
                "timestamp": Date.now().unix
            },
            kwargs
        )

        return dest_key, dest_etl


def coalesce_w_conflict_detection(source_key, *args):
    if len(args) < 2:
        Log.error("bad call to coalesce, expecting source_key as first parameter")

    output = KNOWN_COALESCE_CONFLICTS.get(args, Null)
    if output is not Null:
        return output

    output = Null
    for a in args:
        if a == None:
            continue
        if output == None:
            output = a
        elif a != output:
            Log.warning(
                "tried to coalesce {{values_|json}} while processing {{key}}",
                key=source_key,
                values_=args,
            )
        else:
            pass
    return output


KNOWN_COALESCE_CONFLICTS = {
    (null, null, null, null, null, "mozilla-esr78", null, "comm-esr78"): (
        "mozilla-esr78"
    ),
    (null, null, null, null, null, null, "firefox", null, null, null, "browser",): (
        "firefox"
    ),
    (null, null, null, null, "mozilla-central", null, "comm-central",): (
        "mozilla-central"
    ),
    (null, "thunderbird", null, null, null, null, "firefox", null, null, null, null,): (
        "thunderbird"
    ),
    (null, null, null, null, "mozilla-beta", null, "comm-beta"): "mozilla-beta",
    (null, null, null, null, null, "mozilla-beta", null, "comm-beta"): "mozilla-beta",
    (null, null, null, null, null, "mozilla-central", null, "try-comm-central",): (
        "mozilla-central"
    ),
    (null, null, null, null, null, "mozilla-central", null, "comm-central",): (
        "mozilla-central"
    ),
    (null, null, null, null, null, "mozilla-beta", null, "comm-beta"): "mozilla-beta",
    (null, null, null, null, null, "mozilla-esr60", null, "comm-esr60",): (
        "mozilla-esr60"
    ),
    (null, null, null, null, null, "gecko-dev.git", null, "mozilla-beta",): (
        "gecko-dev.git"
    ),
    (null, null, null, null, null, "gecko-dev.git", null, "mozilla-release",): (
        "gecko-dev.git"
    ),
    (null, null, null, null, null, "try", null, "try-comm-central"): "try",
    ("jsreftest", "reftest"): "jsreftest",
    ("win64-aarch64-devedition", null, "windows2012-aarch64-devedition", null, null,): (
        "win64-aarch64-devedition"
    ),
    ("android-x86_64", null, "android", null, null): "android-x86_64",
    (null, null, null, null, null, null, "thunderbird", null, null, null, "mail",): (
        "thunderbird"
    ),
    (null, null, null, null, null, "mozilla-esr68", null, "comm-esr68"): (
        "mozilla-esr68"
    ),
}

