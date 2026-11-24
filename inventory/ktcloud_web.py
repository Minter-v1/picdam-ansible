#!/usr/bin/env python3
"""Discover WEB instances using credentials supplied by AWX."""

import argparse
import http.client
import ipaddress
import json
import os
import re
import sys

API_HOST = "api.ucloudbiz.olleh.com"


def required(name):
    value = os.environ.get(name, "")
    if not value.strip():
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def request(method, path, body=None, token=None):
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Auth-Token"] = token

    connection = http.client.HTTPSConnection(API_HOST, timeout=30)
    try:
        connection.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers=headers,
        )
        response = connection.getresponse()
        content = response.read()
        if response.status not in (200, 201):
            raise RuntimeError(f"{method} {path}: HTTP {response.status}")
        return response.getheader("X-Subject-Token"), json.loads(content)
    finally:
        connection.close()


def discover():
    pattern = re.compile(required("PICDAM_WEB_NAME_PATTERN"))
    auth = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": required("PICDAM_D1_USERNAME"),
                        "domain": {"id": "default"},
                        "password": required("PICDAM_D1_PASSWORD"),
                    }
                },
            },
            "scope": {
                "project": {"id": required("PICDAM_D1_PROJECT_ID")}
            },
        }
    }

    token, _ = request("POST", "/d1/identity/auth/tokens", body=auth)
    if not token:
        raise RuntimeError("Authentication returned no token.")

    _, data = request("GET", "/d1/server/servers/detail", token=token)
    if not isinstance(data.get("servers"), list):
        raise RuntimeError("Unexpected server list response.")
    if any(
        link.get("rel") == "next"
        for link in (data.get("servers_links") or [])
    ):
        raise RuntimeError("Pagination detected; refusing an incomplete inventory.")

    hosts = {}
    for server in data["servers"]:
        if not pattern.fullmatch(server.get("name", "")):
            continue
        if server.get("status") != "ACTIVE":
            continue

        addresses = {
            address["addr"]
            for network in (server.get("addresses") or {}).values()
            for address in network
            if address.get("version") == 4
            and ipaddress.ip_address(address["addr"]).is_private
            and address.get("OS-EXT-IPS:type") != "floating"
        }
        if len(addresses) != 1:
            raise RuntimeError(
                f"{server['name']}: expected exactly one private IPv4."
            )

        alias = "web_" + server["id"].replace("-", "_")
        hosts[alias] = {
            "ansible_host": addresses.pop(),
            "ansible_user": "ubuntu",
            "ansible_ssh_common_args": (
                "-o BatchMode=yes "
                "-o StrictHostKeyChecking=accept-new "
                f"-o HostKeyAlias={alias}"
            ),
        }

    return {
        "_meta": {"hostvars": hosts},
        "all": {"children": ["ungrouped", "picdam_web"]},
        "ungrouped": {"hosts": []},
        "picdam_web": {"hosts": sorted(hosts)},
    }


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--host")
    args = parser.parse_args()

    inventory = discover()
    result = (
        inventory
        if args.list
        else inventory["_meta"]["hostvars"].get(args.host, {})
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        OSError,
        http.client.HTTPException,
    ) as error:
        print(f"Inventory discovery failed: {error}", file=sys.stderr)
        sys.exit(1)
