import json
from public_api_test.client import PublicComClient
c=PublicComClient()
# Reproduce original endpoint and Bearer-secret usage; never print headers.
c.token=c.api_key
r=c._record('AAPL','/v1/quote',c._request('GET','/v1/quote',params={'symbol':'AAPL'}),method='GET')
print(json.dumps(r,indent=2))
c.close()
