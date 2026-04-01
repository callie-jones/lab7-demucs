#!/usr/bin/env python3

import requests
import json, jsonpickle
import os
import sys
import base64
import glob


#
# Use REST env var, e.g. REST=localhost:5001
# On macOS, port 5000 is often AirPlay Receiver (returns 403) — use 5001 for Flask.
#
REST = os.getenv("REST") or "localhost:5001"
# Webhook target for the worker (must match REST host:port, not AirPlay on :5000)
CALLBACK_BASE = os.getenv("CALLBACK_BASE") or f"http://{REST}"

##
# The following routine makes a JSON REST query of the specified type
# and if a successful JSON reply is made, it pretty-prints the reply
##

def mkReq(reqmethod, endpoint, data, verbose=True):
    print(f"Response to http://{REST}/{endpoint} request is {type(data)}")
    jsonData = jsonpickle.encode(data)
    if verbose and data != None:
        print(f"Make request http://{REST}/{endpoint} with json {data.keys()}")
        print(f"mp3 is of type {type(data['mp3'])} and length {len(data['mp3'])} ")
    response = reqmethod(f"http://{REST}/{endpoint}", data=jsonData,
                         headers={'Content-type': 'application/json'})
    if response.status_code == 200:
        jsonResponse = json.dumps(response.json(), indent=4, sort_keys=True)
        print(jsonResponse)
        return
    else:
        print(
            f"response code is {response.status_code}, raw response is {response.text}")
        return response.text


for mp3 in glob.glob("data/short*.mp3"):
    print(f"Separate {mp3}")
    mkReq(requests.post, "apiv1/separate",
        data={
            "mp3": base64.b64encode( open(mp3, "rb").read() ).decode('utf-8'),
            "callback": {
                "url": CALLBACK_BASE,
                "data": {"mp3": mp3, 
                         "data": "to be returned"}
            }
        },
        verbose=True
        )
    print(f"Cache from server is")
    mkReq(requests.get, "apiv1/queue", data=None)

sys.exit(0)