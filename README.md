# Steam Workshop Backup Server for Natural Selection 2

The Workshop Backup Server ensures that Natural Selection 2 clients will be able to download
a server-side mod even when Steam is acting up OR when the server is running
an out-dated version of a mod.

Its intended use is to make it simple for anyone to host a steam backup server
for their own or others game servers.

To support "swapping" backups between groups of server ops, multiple backup
servers can be listed - as clients accesses the servers in a random order, load
will be automatically distributed over listed servers.

The server requires nearly zero maintenance, as the set of mods backed up comes
from client requests and missing mods are automatically downloaded from steam.

As the server is written in plain python and just extends / restricts the
standard python webserver, it should be easy to verify that it is safe to run.

## Setup

### NS2

If the "mod_backup_servers" variable is not present in ServerConfig.json, start and
stop the server, it should generate the default variables.

In ServerConfig.json, set "mod_backup_servers" to the list of backup servers.

    "mod_backup_servers":[ "http://example.com:27020", "http://example2.com:27020" ],

If you would like people connecting to your server to download directly from the backup
servers without bothering to try steam first (faster at all times, but especially so
during steam sales), you can change set the "mod_backup_before_steam" key to true, ie

   "mod_backup_before_steam":true,

### Backup server

If you use the python script python 3.4+ (https://www.python.org/downloads/) is required!

Copy and paste the workshopbackup.py or workshopbackup.exe file into your game servers Workshop directory. 
Existing mod folders will be automatically zipped by the backup server on demand. 

Run it once to generate the config file. 

Edit the config file after your needs. You will have to at least add a steamworks web api key. For more details about the configuration look
into the configuration section of this readme. And restart it.

Also make sure it can be accessed through any firewalls - as it just uses
the standard HTTP protocol, only TCP needs to be open on the given port.

## Test
You can test the workshop backup server locally via e.g http://localhost:27020/m5f3f7c0_1348957670.zip

Change the URL and port accordingly to your configuration.

The server should respond with 202 error message, refresh it a few times and it
should either return the zip file (or a 403 if the mod
has been updated).

The corresponding .zip file should exist in the directory.


## Configuration

All config parameters can be found in the workshopbackup.json.

| Name                                    | Description                                                                     | Default         |
|-----------------------------------------|---------------------------------------------------------------------------------|-----------------|
| ALLOWED_APP_ID                          | List of allowed app ids to store mods from. Empty = allow all                   | [4920]          |
| ALLOWED_MOD_IDS                         | List all allowed mod ids to store. Empty = allow all                            | []              |
| DISALLOWED_MOD_IDS                      | List all disallowed mod ids to store. Empty = allow all                         | []              |
| INTERFACE                               | Interface to listen on. 0.0.0.0 = all                                           | 0.0.0.0         |
| PORT                                    | Port the server will listen to                                                  | 27020           |
| MAX_OUTSTANDING_STEAM_DOWNLOAD_REQUESTS | Maximum amount of downloads from steam the server will process at the same time | 4               |
| VERSION_OVERLAP_WINDOW                  | How long we preserve a outdated version of a mod in secs                        | 604800 (1 week) |
| LOG                                     | If True trace logging will be enabled                                           | false           |
| API_URL                                 | The Steamworks API url just for fetching the workshop mod details               | https://api.steampowered.com/IPublishedFileService/GetDetails/v1/ |
| API_KEY                                 | The Web API key used for the Steamworks api requests, visit https://steamcommunity.com/dev/apikey to generate your own steamworks api key. | Not set by default |

## Maintenance

The server should require zero maintenance, apart from possibly
restarting the server every 6 month or so just to truncate the
log files.

Mods are downloaded as clients submit requests for them - no need to manually
update anything.

Old mods are cleared out about a week after the latest version comes out.

The server can be restarted at will - it will serve any existing file in the
directory (that matches the m%x_%d pattern), so restarting does not mean
it will have to re-download files from steam.

Stopping the server and removing all files from the directory will reset
the server - possibly useful if there are too many old mods that noone
uses anymore.

The log-err.txt contains both errors and a record of all download requests.

## Expected resource usage

The server should be very resource-conservative. As it will only serve server-side mods,
and clients will only connect to the server if they don't have the mod already, the only
time it will see significant use is

- first time players at a server missing one or more server-side mods (aka steam sale time)
- when a server-side mod is updated.

Note that while most mods weighs in at the few-kb size, maps range in size from 5 to 50Mb.


## Background

That Steam servers serving up mods gets overloaded whenever there are Steam
sales is an unfortunate fact of life. Even during normal days, attempts to
download mod information or mods will fail about one time in four. During sales,
that goes up to about nine out of ten.

The server checks for new versions of a mod on every map change. This means
that when a mod is updated, there will be a time when the version of a mod
that the server is using is simply not available on Steam.

How long the server will run with an outdated version may vary - while it
checks every map change, if steam is acting up it may be upto several hours
before the server actually manages to download the new version.

### Solution

The server acts as a very restricted web server. It ONLY services requests
for files whose pattern is "/m{mod-id-in-hex}_{timestamp/version-in-decimal}.zip".

If a file matching that pattern is present, it is served via the usual http-protocol.

If the request does not match the pattern, it is 404'ed.

If the request DOES match the pattern, but there is no such file, a 202 (request accepted,
come back later) is returned.

If a folder fitting the given mod token exists it will be zipped on demand.

Otherwise a request is sent to the steam-downloader part of the server.

The steam downloader will (in the background) try to download information about the
given mod_id. Once steam answers, the response is analysed and stored away (persisted
in the content.json file), and if the mod should be served, downloaded and stored in
the current directory.

To avoid spamming steam too much, the server keeps track of invalid requests so multiple
requests to outdated, unserved or otherwise invalid mods are denied efficiently.

This means that the set of mods being backed up will automatically adjust to the set
of mods actually being used by servers backed up, without any need for manual updating.
