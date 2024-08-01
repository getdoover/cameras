import re
#
SNAPSHOT_MATCH = re.compile(b"Content-Type: (?P<content>.*)\r\n"
                            b"Content-Length:(?P<content_length>.*)\r\n\r\n"
                            b"(?P<data>.*)", re.DOTALL)

fmt = b'Content-Type: text/plain\r\nContent-Length:9\r\n\r\nHeartbeat'

m = SNAPSHOT_MATCH.search(fmt)
print(m.group("data"))
# import requests
# from requests.auth import HTTPDigestAuth
#
# url = "http://192.168.0.102/cgi-bin/snapshot.cgi?channel=1"
# auth = HTTPDigestAuth('admin', '19HandleyDrive')
#
# response = requests.get(url, auth=auth)
# print(response.content)
# with open("test.jpg", "wb") as fp:
#     fp.write(response.content)