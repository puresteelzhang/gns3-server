#!/usr/bin/env python
#
# Copyright (C) 2016 GNS3 Technologies Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import aiohttp
import asyncio
import json
import uuid
import os
import io

from ..utils import parse_version
from ..utils.images import scan_for_images
from ..controller.controller_error import ControllerError
from ..config import Config
from ..version import __version__


import logging
log = logging.getLogger(__name__)


class ComputeError(ControllerError):
    pass


class ComputeConflict(aiohttp.web.HTTPConflict):
    """
    Raise when the compute send a 409 that we can handle

    :param response: The response of the compute
    """

    def __init__(self, response):
        super().__init__(text=response["message"])
        self.response = response


class Timeout(aiohttp.Timeout):
    """
    Could be removed with aiohttp 0.22 that support None timeout
    """

    def __enter__(self):
        if self._timeout:
            return super().__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._timeout:
            return super().__exit__(exc_type, exc_val, exc_tb)
        return self


class Compute:
    """
    A GNS3 compute.
    """

    def __init__(self, compute_id, controller=None, protocol="http", host="localhost", port=3080, user=None, password=None, name=None):
        self._http_session = None
        assert controller is not None
        log.info("Create compute %s", compute_id)

        if compute_id is None:
            self._id = str(uuid.uuid4())
        else:
            self._id = compute_id

        self.protocol = protocol
        self.host = host
        self.port = port
        self._user = None
        self._password = None
        self._connected = False
        self._controller = controller
        self._set_auth(user, password)
        self._version = None
        self._cpu_usage_percent = None
        self._memory_usage_percent = None
        self.name = name
        # Websocket for notifications
        self._ws = None

        # It's a configuration issue if the compute is not configured to be local but the compute id is local
        if compute_id == "local" and Config.instance().get_section_config("Server")["local"] is False:
            raise ComputeError("The local compute is started without --local")

    def _session(self):
        if self._http_session is None or self._http_session.closed is True:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    def __del__(self):
        if self._http_session:
            self._http_session.close()

    def _set_auth(self, user, password):
        """
        Set authentication parameters
        """
        if user is None or len(user.strip()) == 0:
            self._user = None
            self._password = None
            self._auth = None
        else:
            self._user = user.strip()
            if password:
                self._password = password.strip()
                self._auth = aiohttp.BasicAuth(self._user, self._password)
            else:
                self._password = None
                self._auth = aiohttp.BasicAuth(self._user, "")

    @asyncio.coroutine
    def update(self, **kwargs):
        for kw in kwargs:
            setattr(self, kw, kwargs[kw])
        if self._http_session:
            self._http_session.close()
        self._connected = False
        self._controller.notification.emit("compute.updated", self.__json__())
        self._controller.save()

    @asyncio.coroutine
    def close(self):
        self._connected = False
        if self._http_session:
            self._http_session.close()
        if self._ws:
            yield from self._ws.close()
            self._ws = None

    @property
    def version(self):
        """
        :returns: Version of compute node (string or None if not connected)
        """
        return self._version

    @property
    def name(self):
        """
        :returns: Compute name
        """
        return self._name

    @name.setter
    def name(self, name):
        if name is not None:
            self._name = name
        elif self._id == "local":
            self._name = "Local"
        else:
            if self._user:
                user = self._user
                # Due to random user generated by 1.4 it's common to have a very long user
                if len(user) > 14:
                    user = user[:11] + "..."
                self._name = "{}://{}@{}:{}".format(self._protocol, user, self._host, self._port)
            else:
                self._name = "{}://{}:{}".format(self._protocol, self._host, self._port)

    @property
    def connected(self):
        """
        :returns: True if compute node is connected
        """
        return self._connected

    @property
    def id(self):
        """
        :returns: Compute identifier (string)
        """
        return self._id

    @property
    def host(self):
        """
        :returns: Compute host (string)
        """
        return self._host

    @host.setter
    def host(self, host):
        self._host = host

    @property
    def port(self):
        """
        :returns: Compute port (integer)
        """
        return self._port

    @port.setter
    def port(self, port):
        self._port = port

    @property
    def protocol(self):
        """
        :returns: Compute protocol (string)
        """
        return self._protocol

    @protocol.setter
    def protocol(self, protocol):
        self._protocol = protocol

    @property
    def user(self):
        return self._user

    @user.setter
    def user(self, value):
        self._set_auth(value, self._password)

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        self._set_auth(self._user, value)

    @property
    def cpu_usage_percent(self):
        return self._cpu_usage_percent

    @property
    def memory_usage_percent(self):
        return self._memory_usage_percent

    def __json__(self, topology_dump=False):
        """
        :param topology_dump: Filter to keep only properties require for saving on disk
        """
        if topology_dump:
            return {
                "compute_id": self._id,
                "name": self._name,
                "protocol": self._protocol,
                "host": self._host,
                "port": self._port
            }
        return {
            "compute_id": self._id,
            "name": self._name,
            "protocol": self._protocol,
            "host": self._host,
            "port": self._port,
            "user": self._user,
            "connected": self._connected,
            "cpu_usage_percent": self._cpu_usage_percent,
            "memory_usage_percent": self._memory_usage_percent
        }

    @asyncio.coroutine
    def download_file(self, project, path):
        """
        Read file of a project and download it

        :param project: A project object
        :param path: The path of the file in the project
        :returns: A file stream
        """

        url = self._getUrl("/projects/{}/files/{}".format(project.id, path))
        response = yield from self._session().request("GET", url, auth=self._auth)
        if response.status == 404:
            raise aiohttp.web.HTTPNotFound(text="{} not found on compute".format(path))
        return response.content

    @asyncio.coroutine
    def stream_file(self, project, path):
        """
        Read file of a project and stream it

        :param project: A project object
        :param path: The path of the file in the project
        :returns: A file stream
        """

        url = self._getUrl("/projects/{}/stream/{}".format(project.id, path))
        response = yield from self._session().request("GET", url, auth=self._auth)
        if response.status == 404:
            raise aiohttp.web.HTTPNotFound(text="{} not found on compute".format(path))
        return response.content

    @asyncio.coroutine
    def http_query(self, method, path, data=None, **kwargs):
        if not self._connected:
            yield from self._connect()
        if not self._connected:
            raise aiohttp.web.HTTPConflict(text="The server {} is not a GNS3 server".format(self._id))
        response = yield from self._run_http_query(method, path, data=data, **kwargs)
        return response

    @asyncio.coroutine
    def _connect(self):
        """
        Check if remote server is accessible
        """
        if not self._connected:
            response = yield from self._run_http_query("GET", "/version")

            if "version" not in response.json:
                self._http_session.close()
                raise aiohttp.web.HTTPConflict(text="The server {} is not a GNS3 server".format(self._id))
            self._version = response.json["version"]
            if parse_version(__version__)[:2] != parse_version(response.json["version"])[:2]:
                self._http_session.close()
                raise aiohttp.web.HTTPConflict(text="The server {} versions are not compatible {} != {}".format(self._id, __version__, response.json["version"]))

            self._notifications = asyncio.gather(self._connect_notification())
            self._connected = True
            self._controller.notification.emit("compute.updated", self.__json__())

    @asyncio.coroutine
    def _connect_notification(self):
        """
        Connect to the notification stream
        """
        self._ws = yield from self._session().ws_connect(self._getUrl("/notifications/ws"))
        while True:
            response = yield from self._ws.receive()
            if response.tp == aiohttp.MsgType.closed or response.tp == aiohttp.MsgType.error:
                self._connected = False
                break
            msg = json.loads(response.data)
            action = msg.pop("action")
            event = msg.pop("event")

            if action == "ping":
                self._cpu_usage_percent = event["cpu_usage_percent"]
                self._memory_usage_percent = event["memory_usage_percent"]
                self._controller.notification.emit("compute.updated", self.__json__())
            else:
                self._controller.notification.dispatch(action, event, compute_id=self.id)
        yield from self._ws.close()
        self._ws = None

    def _getUrl(self, path):
        return "{}://{}:{}/v2/compute{}".format(self._protocol, self._host, self._port, path)

    @asyncio.coroutine
    def _run_http_query(self, method, path, data=None, timeout=10, raw=False):
        with Timeout(timeout):
            url = self._getUrl(path)
            headers = {}
            headers['content-type'] = 'application/json'
            chunked = False
            if data == {}:
                data = None
            elif data is not None:
                if hasattr(data, '__json__'):
                    data = json.dumps(data.__json__())
                # Stream the request
                elif isinstance(data, aiohttp.streams.StreamReader) or isinstance(data, io.BufferedIOBase) or isinstance(data, bytes):
                    chunked = True
                    headers['content-type'] = 'application/octet-stream'
                else:
                    data = json.dumps(data)

        response = yield from self._session().request(method, url, headers=headers, data=data, auth=self._auth, chunked=chunked)
        body = yield from response.read()
        if body and not raw:
            body = body.decode()

        if response.status >= 300:
            # Try to decode the GNS3 error
            if body and not raw:
                try:
                    msg = json.loads(body)["message"]
                except (KeyError, ValueError):
                    msg = body
            else:
                msg = ""

            if response.status == 400:
                raise aiohttp.web.HTTPBadRequest(text="Bad request {} {}".format(url, body))
            elif response.status == 401:
                raise aiohttp.web.HTTPUnauthorized(text="Invalid authentication for compute {}".format(self.id))
            elif response.status == 403:
                raise aiohttp.web.HTTPForbidden(text=msg)
            elif response.status == 404:
                raise aiohttp.web.HTTPNotFound(text=msg)
            elif response.status == 409:
                try:
                    raise ComputeConflict(json.loads(body))
                # If the 409 doesn't come from a GNS3 server
                except ValueError:
                    raise aiohttp.web.HTTPConflict(text=msg)
            elif response.status == 500:
                raise aiohttp.web.HTTPInternalServerError(text="Internal server error {}".format(url))
            elif response.status == 503:
                raise aiohttp.web.HTTPServiceUnavailable(text="Service unavailable {} {}".format(url, body))
            else:
                raise NotImplementedError("{} status code is not supported".format(response.status))
        if body and len(body):
            if raw:
                response.body = body
            else:
                try:
                    response.json = json.loads(body)
                except ValueError:
                    raise aiohttp.web.HTTPConflict(text="The server {} is not a GNS3 server".format(self._id))
        else:
            response.json = {}
            response.body = b""
        return response

    @asyncio.coroutine
    def get(self, path, **kwargs):
        return (yield from self.http_query("GET", path, **kwargs))

    @asyncio.coroutine
    def post(self, path, data={}, **kwargs):
        response = yield from self.http_query("POST", path, data, **kwargs)
        return response

    @asyncio.coroutine
    def put(self, path, data={}, **kwargs):
        response = yield from self.http_query("PUT", path, data, **kwargs)
        return response

    @asyncio.coroutine
    def delete(self, path, **kwargs):
        return (yield from self.http_query("DELETE", path, **kwargs))

    @asyncio.coroutine
    def forward(self, method, type, path, data=None):
        """
        Forward a call to the emulator on compute
        """
        res = yield from self.http_query(method, "/{}/{}".format(type, path), data=data, timeout=None)
        return res.json

    @asyncio.coroutine
    def images(self, type):
        """
        Return the list of images available for this type on controller
        and on the compute node.
        """
        images = []

        res = yield from self.http_query("GET", "/{}/images".format(type), timeout=120)
        images = res.json

        if type in ["qemu", "dynamips", "iou"]:
            for path in scan_for_images(type):
                image = os.path.basename(path)
                if image not in [i['filename'] for i in images]:
                    images.append({"filename": image, "path": image})
        return images

    @asyncio.coroutine
    def list_files(self, project):
        """
        List files in the project on computes
        """
        path = "/projects/{}/files".format(project.id)
        res = yield from self.http_query("GET", path, timeout=120)
        return res.json