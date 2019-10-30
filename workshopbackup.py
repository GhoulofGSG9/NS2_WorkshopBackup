#
# workshopBackup - a simple system for keeping a backup of steam mods for downloading when steam acts up
# Copyright 2015 Mats Olsson (mats.olsson@matsotech.se)
#
# License agreement TBD - just something free
#
# ----
# Does three things:
# - A simple web server only serving full-name static zip files from a single directory.
# -- if request comes in for something not stored in the cache, requests info from steam about the mod
# **  if the mod info comes back and the mod should be downloaded (exists and is among the apps we cover),
# it is downloaded from steam
#
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
from operator import attrgetter
import os
import shutil
from socketserver import ThreadingMixIn
from threading import Thread, Condition
import urllib
import sys
import time
from zipfile import ZIP_DEFLATED, ZipFile

try:
    import requests
except ImportError:
    print("Please install or update the Requests module.")
    print("You can do so by running 'pip install requests' in your console")
    sys.exit(1)

VERSION = "0.6.0"

#
# CONFIGURE BLOCK

DEFAULTCONFIG = {
    'ALLOWED_APP_IDS': [4920],  # list all allowed app ids to store. Empty = allow all
    'ALLOWED_MOD_IDS': [],  # list all allowed mod ids to store. Empty = allow all
    'DISALLOWED_MOD_IDS': [],  # list all disallowed mod ids to store. Empty = allow all
    'MONITOR_MOD_IDS': [], # list of mod ids to monitor (make sure we always have the last version)
    'MONITOR_INTERVAL': 600, # interval in seconds at which we check the monitored mods
    'INTERFACE': '0.0.0.0',  # interface to listen on. 0.0.0.0 = all
    'PORT': 27020,  # what port to listen to
    'MAX_OUTSTANDING_STEAM_DOWNLOAD_REQUESTS': 4,  # number of downloads from steam we will have at the same time
    'VERSION_OVERLAP_WINDOW': 7 * 24 * 3600,
    # How old the latest version must be before we remove older version (1 week in seconds)
    'LOG': False,  # turn on trace logging
    'API_URL': 'https://api.steampowered.com/IPublishedFileService/GetDetails/v1/',
    # URL for fetching the mod details from the steamworks api
    'API_KEY': '',
    # api key used for all api requests, visit https://steamcommunity.com/dev/apikey to generate your own steamworks api key.
}

CONFIG = {}

config_filename = "workshopbackup.json"

if os.path.exists(config_filename):
    with open(config_filename) as json_file:
        json_data = json.load(json_file)
        for i, v in DEFAULTCONFIG.items():
            if i in json_data:
                CONFIG[i] = json_data[i]
            else:
                CONFIG[i] = DEFAULTCONFIG[i]
else:
    CONFIG = DEFAULTCONFIG

with open(config_filename, 'w') as f:
    f.write(json.dumps(CONFIG, indent=4))

# END CONFIGURE BLOCK
#


def log(msg):
    if CONFIG['LOG']:
        print(msg)


def check_path(path):
    if path.startswith("/m"):
        path = path[2:]
    elif path.startswith("//m"):
        path = path[3:]
    else:
        return False

    parts = path.split(".")
    if len(parts) != 2 or parts[1] != "zip":
        return False
    parts = parts[0].split("_")
    if len(parts) != 2:
        return False
    mod_id = int(parts[0], 16)
    version = int(parts[1])
    return mod_id, version


def make_key(mod_id, version):
    """database key for a mod_id/version tuple"""
    return 'm%x_%d' % (mod_id, version)


def now_millis():
    return int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds() * 1000)


def handle_error(msg):
    error_type, error_msg, traceback = sys.exc_info()
    log("caught error '%s' %s" % (error_type, msg))
    sys.excepthook(error_type, error_msg, traceback)


class ModInfo(object):
    maxRetries = 10
    timeoutPerTry = 20

    def __init__(self, id=None, version=None, xml_node=None, json_node=None):
        self.id = 0
        self.version = 0
        self.title = "-"
        self.app_id = 0
        self.size = 0
        self.url = ""
        self.last_checked = now_millis()
        self.exists = True
        self.allowed = True
        self.info_loaded = False

        if xml_node:
            self.init_from_xml(xml_node)
        elif json_node:
            self.init_from_json(json_node)
        else:
            self.id = id
            self.version = version

        self.filename = 'm%x_%d.zip' % (self.id, self.version)
        log("created %s" % self.tostring())

    def __str__(self):
        return "mod[%x_%d]=%s" % (self.id, self.version, self.title)

    def init_from_xml(self, node):
        self.id = int(node.findtext('publishedfileid'))
        result = int(node.findtext('result'))
        if result == 9:
            self.exists = False
        else:
            self.version = int(node.findtext('time_updated'))
            self.title = node.findtext('title')
            self.app_id = int(node.findtext('consumer_app_id') or node.findtext('consumer_appid'))
            self.size = int(node.findtext('file_size'))
            self.url = node.findtext('file_url')
            self.exists = True
            self.allowed = not CONFIG['ALLOWED_APP_IDS'] or self.app_id in CONFIG['ALLOWED_APP_IDS']
            self.info_loaded = True

    def init_from_json(self, node):
        self.id = int(node['publishedfileid'])
        result = int(node['result'])

        if result == 9:
            self.exists = False
        else:
            self.version = int(node['time_updated'])
            self.title = node['title']
            if 'consumer_app_id' in node:
                self.app_id = int(node['consumer_app_id'])
            else:
                self.app_id = int(node['consumer_appid'])
            self.size = int(node['file_size'])
            self.url = node['file_url']
            self.exists = True
            self.allowed = not CONFIG['ALLOWED_APP_IDS'] or self.app_id in CONFIG['ALLOWED_APP_IDS']
            self.info_loaded = True

    def is_downloaded(self):
        return os.path.exists(self.filename)

    def should_download(self):
        return self.exists and self.allowed and self.url != "" and not self.is_downloaded()

    def is_valid(self):
        return self.exists and self.allowed

    def age(self):
        """Return age in seconds"""
        return (now_millis() - self.last_checked) / 1000.0

    def tostring(self):
        return "mod['%s'(app %d), age %s sec, size %d bytes, m%x_%d]" % (
            self.title, self.app_id, self.age(), self.size, self.id, self.version)

    def key(self):
        return make_key(self.id, self.version)

    def download(self):
        temp_filename = '%s.downloading' % self.filename
        if os.path.exists(temp_filename):
            # someone else is already trying to download it .. just warn and overwrite it...
            log("%s already exists?" % temp_filename)
        try:
            request = requests.get(self.url)
            with open(temp_filename, 'wb') as fd:
                for chunk in request.iter_content(chunk_size=128):
                    fd.write(chunk)
            os.rename(temp_filename, self.filename)
            return request.status_code
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    def delete_file(self):
        if os.path.exists(self.filename):
            os.remove(self.filename)


class ModDetailEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, ModInfo):
            return obj.__dict__
        return json.JSONEncoder.default(self, obj)


class DownloadModInfoRequest(object):
    """Wraps a download info from steam request"""
    detailsUrl = CONFIG['API_URL']

    def __init__(self, ids):
        self.mod_ids = ids

    def download(self, timeout=10):
        log("info request started for %s" % ["m%x" % id for id in self.mod_ids])
        result = []
        raw_args = []

        if CONFIG['API_KEY'] != '':
            raw_args.append(("key", CONFIG['API_KEY']))
        else:
            log("warning: API_KEY is not set!")

        for index, id in enumerate(self.mod_ids):
            raw_args.append(("publishedfileids[%d]" % index, str(id)))
        raw_args.append(("itemcount", str(len(self.mod_ids))))

        request = requests.get(self.detailsUrl, params=raw_args)
        if request.status_code == 200:
            json_data = request.json()
            if json_data['response'] and json_data['response']['publishedfiledetails']:
                for file_node in json_data['response']['publishedfiledetails']:
                    result.append(ModInfo(json_node=file_node))
        else:
            log("failed to fetch mod details from the steamworks api. Response code: %s" % request.status_code)

        return result


class Zipper(object):
    """Zips existing mod folders, usefull when the backup server is run from inside a game servers workshop folder"""
    def __init__(self, mod):
        self.mod = mod
        self.thread = Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        log("zipping started for %s" % self.mod)
        if not os.path.isdir(self.mod):
            log("couldn't find %s folder anymore aborting zipping process!" % self.mod)
            return

        tempfilename = "%s.zip.zipping" % self.mod
        if os.path.exists(tempfilename):
            log("%s is allready getting zipped aborting zipping process!" % self.mod)
            return

        with ZipFile(tempfilename, "w", ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(self.mod):
                # NOTE: ignores empty directories
                for fn in files:
                    absfn = os.path.join(root, fn)
                    zfn = absfn[len(self.mod) + len(os.sep):]  # relative path
                    z.write(absfn, zfn)

        if os.path.exists(tempfilename):
            os.rename(tempfilename, "%s.zip" % self.mod)
        log("zipping finished for %s" % self.mod)


class DownloadModRequest(object):
    def __init__(self, database, mod):
        self.database = database
        self.mod = mod
        self.thread = Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        log("download started for %s" % self.mod)
        response = self.mod.download()
        self.database.download_completed(self, response)
        log("download finished for %s" % self.mod)


class ProducerThread(object):
    def __init__(self):
        self.condition = Condition()
        self.is_shutdown = False

    def shutdown(self):
        log("shutting down %s" % self)
        with self.condition:
            self.is_shutdown = True
            self.condition.notify_all()
        log("shut down %s" % self)

    def run(self):
        log("start %s" % self)
        while not self.is_shutdown:
            with self.condition:
                while not self.something_todo() and not self.is_shutdown:
                    self.condition.wait()
                self.flush()
            self.post_flush()
        log("exit %s" % self)

    def something_todo(self):
        """Return true if there is something todo"""
        pass

    def flush(self):
        """override to flush incoming stored data inside the condition"""
        pass

    def post_flush(self):
        """process flushed data outside condition"""
        pass


class SteamInfoDownloader(ProducerThread):
    """Downloads info from steam"""

    def __init__(self, mod_database):
        super().__init__()
        self.mod_database = mod_database
        self.incomingRequests = []
        self.corruptedRequest = []
        self.requests = set()

    def add_requests(self, requests):
        """Add list of requests to current requests"""
        with self.condition:
            self.incomingRequests.extend(requests)
            self.condition.notify_all()

    def something_todo(self):
        return self.incomingRequests

    def flush(self):
        self.requests.update(self.incomingRequests)
        self.incomingRequests.clear()

    def post_flush(self):
        # avoid re-downloading all valid recently loaded mods
        removals = []
        for id in self.requests:
            mod = self.mod_database.get_latest_mod(id)
            if mod and mod.is_valid() and mod.age() < 60:
                removals.append(id)
        self.requests.difference_update(removals)

        # do one attempt to download the mods. Any successfully download mods are removed from
        # the active set of mod ids. Failing will put them back
        if self.requests:
            # noinspection PyBroadException,PyUnresolvedReferences
            try:
                mods = DownloadModInfoRequest(self.requests).download()
                for mod in mods:
                    self.mod_database.info_completed(mod)
                    self.requests.discard(mod.id)

                with self.condition:
                    self.incomingRequests.extend(self.requests)

            except urllib.error.URLError as e:
                print(e.reason)
            except:
                log('failed to downloading details for %s' % self.requests)

            self.requests.clear()

class ModsMonitor(ProducerThread):
    """Monitors the in the config specified mods for updates
    making sure the backup server always has the last version downloaded"""

    def __init__(self, mod_database):
        super().__init__()
        self.mod_database = mod_database

        """Convert modids hex strings into ints"""
        self.mod_ids = []
        for mod_id in CONFIG['MONITOR_MOD_IDS']:
            self.mod_ids.append(int(mod_id, 16))

        self.__nextRun = 0

    def flush(self):
        log("checking mods %s for updates" % ["m%x" % id for id in self.mod_ids])
        self.mod_database.request_mods(self.mod_ids)
        self.__nextRun = time.time() + CONFIG['MONITOR_INTERVAL']

    def something_todo(self):
        return self.__nextRun < time.time()

class ModDatabase(ProducerThread):
    # as we keep a record of both known good mods to backup and requests for mods we don't backup, we need to ensure
    # that we don't have too many invalid entries in the database. This sets the limit.
    MAX_UNKNOWN_ENTRIES = 1000

    def __init__(self, server):
        super().__init__()
        self.server = server
        self.requests = set()
        self.saved_mod_map_version = 0
        self.incoming_mod_requests = []
        self.incoming_completed_infos = []
        self.incoming_completed_downloads = []
        self.completed_download_requests = []
        self.active_download_request = []
        self.download_requests = []
        self.mod_map = {}

        self.thread = Thread(target=self.run, daemon=True)
        self.steam_downloader = SteamInfoDownloader(self)
        self.steam_downloader_thread = Thread(target=self.steam_downloader.run, daemon=True)

        self.run_mods_monitor = len(CONFIG['MONITOR_MOD_IDS']) > 0
        if self.run_mods_monitor:
            self.mods_monitor = ModsMonitor(self)
            self.mods_monitor_thread = Thread(target=self.mods_monitor.run, daemon=True)

    def start(self):
        self.steam_downloader_thread.start()
        self.thread.start()

        if self.run_mods_monitor:
            self.mods_monitor_thread.start()

    def shutdown(self):
        self.steam_downloader.shutdown()
        self.steam_downloader_thread.join()

        if self.run_mods_monitor:
            self.mods_monitor_thread.shutdown()
            self.mods_monitor_thread.join()

        ProducerThread.shutdown(self)

    def something_todo(self):
        return self.incoming_completed_infos or self.incoming_completed_downloads or self.incoming_mod_requests

    def info_completed(self, mod):
        with self.condition:
            self.incoming_completed_infos.append(mod)
            self.condition.notify_all()

    def download_completed(self, mod, response):
        """called from the DownloadModRequest when a mod has completed downloading"""
        log("download complete for %s, response %s" % (mod, response))
        with self.condition:
            self.incoming_completed_downloads.append((mod, response))
            self.condition.notify_all()

    def request_mod(self, mod_id):
        with self.condition:
            self.incoming_mod_requests.append(mod_id)
            self.condition.notify_all()

    def request_mods(self, mod_ids):
        with self.condition:
            self.incoming_mod_requests.extend(mod_ids)
            self.condition.notify_all()

    def get_all_mods(self, mod_id):
        """Return all versions of the given mod or None"""
        with self.condition:
            self.flush()
            return [v for k, v in self.mod_map.items() if v.id == mod_id]

    def get_mod(self, mod_id, version):
        """Return a specific version of a mod or None"""
        with self.condition:
            self.flush()
            return self.mod_map.get(make_key(mod_id, version))

    def get_latest_mod(self, mod_id):
        """Return the latest version of the mod (or None)"""
        result = None
        with self.condition:
            self.flush()
            for m in (v for k, v in self.mod_map.items() if v.id == mod_id):
                result = (result and m.version < result.version and result) or m
        return result

    def flush(self):
        """flush all the incoming stuff (inside condition) in preparation for working on them."""
        self.steam_downloader.add_requests(self.incoming_mod_requests)
        self.incoming_mod_requests.clear()

        for mod in self.incoming_completed_infos:
            self.mod_map[mod.key()] = mod
            if mod.should_download():
                self.download_requests.append(mod)
        self.incoming_completed_infos.clear()

        self.completed_download_requests.extend(self.incoming_completed_downloads)
        self.incoming_completed_downloads.clear()

        self.cleanup_mod_map()

    def cleanup_mod_map(self):
        """We save up invalid requests to avoid spending time asking steam about them."""
        removals = []
        allowed = self.MAX_UNKNOWN_ENTRIES
        for key, mod in self.mod_map.items():
            if not mod.is_valid():
                if allowed <= 0:
                    removals.append(key)
                else:
                    allowed -= 1
        # we don't really care about what invalid entries we are removing - either the "working set" of
        # unknowns are big enough or someone is just spamming us with random stuff anyhow.
        # Fix it if it becomes a problem, but ...
        for key in removals:
            del self.mod_map[key]

    def post_flush(self):

        for req, response in self.completed_download_requests:
            try:
                self.active_download_request.remove(req)
            except ValueError as e:
                print(e)
            with self.condition:
                self.mod_map[req.mod.key()] = req.mod
        self.completed_download_requests.clear()

        while self.download_requests and len(self.active_download_request) < \
                CONFIG['MAX_OUTSTANDING_STEAM_DOWNLOAD_REQUESTS']:
            mod = self.download_requests.pop()
            if not mod.is_downloaded():
                req = DownloadModRequest(self, mod)
                self.active_download_request.append(req)
                req.start()

        self.cleanup_old_mods()

    def cleanup_old_mods(self):
        mod_db = {}
        for mod in self.mod_map.values():
            mod_db.setdefault(mod.id, []).append(mod)

        cutofftime = time.time() - CONFIG['VERSION_OVERLAP_WINDOW']
        for mod_list in mod_db.values():
            if len(mod_list) > 1:
                mod_list.sort(key=attrgetter('version'), reverse=True)
                if mod_list[0].version < cutofftime:
                    for m in mod_list[1:]:
                        log("cleaning up %s" % m)
                        m.delete_file()
                        del (self.mod_map[m.key()])


class RequestHandler(SimpleHTTPRequestHandler):
    """Custom request handler.
     1. Verify that the request is on the limited format we support ("/m%x_%d.zip")
     2. If the file is available, send it back
     3. If the file is not available, initiate an attempt to download it from steam (server.add_mod_request)
    """

    def pre_check(self):

        # make sure the path EXACTLY matches "m%x_%d.zip"
        result = check_path(self.path)
        if result:
            mod_id, version = result
            modtoken = "m%x_%d" % (mod_id, version)
            filename = "%s.zip" % modtoken
            str_mod_id = str(hex(mod_id))[2:]

            if CONFIG['ALLOWED_MOD_IDS'] and not (str_mod_id in CONFIG['ALLOWED_MOD_IDS']):
                log("reject request for m%s_%d, mod %s not allowed" % (str_mod_id, version, str_mod_id))
                self.send_error(403, Server.RESULT_DENIED_MOD_ID)
                return False

            if CONFIG['DISALLOWED_MOD_IDS'] and str_mod_id in CONFIG['DISALLOWED_MOD_IDS']:
                log("reject request for m%s_%d, mod %s not allowed" % (str_mod_id, version, str_mod_id))
                self.send_error(403, Server.RESULT_DENIED_MOD_ID)
                return False

            # check for file
            if os.path.isfile(filename):
                log("Found %s" % filename)
                return True

            # Check for folder with same name
            if os.path.isdir(modtoken):
                self.send_error(202, "Found mod folder, zipping it up now")
                if not os.path.isfile("%s.zip.zipping" % modtoken):
                    zipper = Zipper(modtoken)
                    zipper.start()
                return False

            # the version asked for does not exist, we need to add it to our requests
            status, result = self.server.add_mod_request(mod_id, version)
            if status != 200:
                self.send_error(status, result)
                return False

            # asking for it made it available somehow
            return True

        log("failed check_path")
        self.send_error(404, "Request not on the correct format")
        return False

    def do_HEAD(self):
        if self.pre_check():
            SimpleHTTPRequestHandler.do_HEAD(self)

    def do_GET(self):
        if self.pre_check():
            SimpleHTTPRequestHandler.do_GET(self)

    def log_happymessage(self, format, *args):
        sys.stdout.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def log_request(self, code='-', size='-'):
        self.log_happymessage('"%s" %s %s', self.requestline, str(code), str(size))


# noinspection PyBroadException
def move_to_old(base, ext):
    try:
        name = '%s.%s' % (base, ext)
        if os.path.exists(name):
            old_name = '%s.old.%s' % (base, ext)
            shutil.copyfile(name, old_name)
    except:
        pass


class Logger(object):
    def __init__(self, filename, std):
        self.terminal = std
        self.log = open(filename, "wt", buffering=1, encoding='utf-8')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


class Server(ThreadingMixIn, HTTPServer):
    RESULT_DOWNLOADING = "Downloading"
    RESULT_DENIED_APP_ID = "Denied - wrong app_id"
    RESULT_DENIED_MOD_ID = "Denied - not serving that mod"
    RESULT_DENIED_UNAVAILABLE = "Denied - old version and not backed up"
    RESULT_DENIED_UNAVAILABLE_VERSION = "Denied - no such version"
    RESULT_DENIED_NO_SUCH_MOD = "Denied - no such mod"

    def __init__(self, address, handler=None):

        # move old logs
        move_to_old('log', 'txt')
        move_to_old('log-err', 'txt')
        # tee stdout/stderr to log.txt and log-err.txt
        sys.stderr = Logger('log-err.txt', sys.stderr)
        sys.stdout = Logger('log.txt', sys.stdout)

        self.mod_database = ModDatabase(self)
        self.handler = handler or RequestHandler

        HTTPServer.__init__(self, address, self.handler)

    def serve_forever(self, poll_interval=0.5):
        self.mod_database.start()
        HTTPServer.serve_forever(self, poll_interval)

    def shutdown(self):
        self.mod_database.shutdown()
        HTTPServer.shutdown(self)

    def start_download(self, mod_id):
        self.mod_database.request_mod(mod_id)
        return 202, self.RESULT_DOWNLOADING

    def get_mod(self, mod_id, version):
        return self.mod_database.get_mod(mod_id, version)

    def get_latest_mod(self, mod_id):
        return self.mod_database.get_latest_mod(mod_id)

    def add_mod_request(self, mod_id, version):
        """ The given mod is not available on the filesystem, make sure we try to download it
        :param mod_id:
        :param version:
        :return: user-information about status "Downloading, Denied" etc (could add a progress to download?)
        """
        mod = self.get_mod(mod_id, version)

        if mod is None:
            mod = self.get_latest_mod(mod_id)

        if mod is None:
            return self.start_download(mod_id)

        if not mod.exists:
            return 404, self.RESULT_DENIED_NO_SUCH_MOD

        if not mod.allowed:
            return 403, self.RESULT_DENIED_APP_ID

        if not mod.info_loaded:
            return 202, self.RESULT_DOWNLOADING

        if mod.version > version:
            # that mod exists in a later version, but not in the exact version - can't download it
            return 404, self.RESULT_DENIED_UNAVAILABLE

        if mod.version < version:
            # asking for a later version than what we have... check age
            if mod.age() < 60:
                return 404, self.RESULT_DENIED_UNAVAILABLE
            else:
                return self.start_download(mod_id)

        # the right version must have showed up while we were waiting
        return 202, self.RESULT_DOWNLOADING


if __name__ == "__main__":
    log("Backup server (version: %s) running on %s:%d, serving from %s" % (
        VERSION, CONFIG['INTERFACE'], CONFIG['PORT'], os.path.abspath(os.curdir)))

    if CONFIG['ALLOWED_APP_IDS']:
        log("Allowing only app_ids: %s" % str(CONFIG['ALLOWED_APP_IDS']).strip('[]'))
    else:
        log("Allowing all app_ids")

    if CONFIG['ALLOWED_MOD_IDS']:
        log("Allowing only mod_ids: %s" % ", ".join(CONFIG['ALLOWED_MOD_IDS']))
    else:
        log("Allowing all mod_ids")

    Server((CONFIG['INTERFACE'], CONFIG['PORT'])).serve_forever()
