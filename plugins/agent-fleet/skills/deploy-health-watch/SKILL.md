---
name: deploy-health-watch
description: Poll a list of health endpoints after a deploy and report any that are unhealthy. Invoke when a deploy has just finished or when asked to watch service health; pair with /loop for 30 to 60 minutes to catch delayed failures.
user-invocable: true
---

# Deploy Health Watch

Check a set of health endpoints and report anything unhealthy. Uses `curl`.

## Endpoints

Take the list from, in order:

1. Arguments: `/deploy-health-watch <url> [<url> ...]`.
2. A project file the user keeps, for example `.health-endpoints` with one URL
   per line (lines starting with `#` ignored).
3. Otherwise ask the user once for the URLs and suggest saving them to that
   file.

## Check

For each URL:

```sh
curl -sS -o /dev/null -w "%{http_code} %{time_total}" --max-time 10 <url>
```

Healthy means HTTP 2xx within the timeout. If the endpoint returns JSON with a
version field (for example a commit SHA), report it too, so a stale deploy is
visible.

## Report

- All healthy: one line, `All N endpoints healthy` (with versions if present).
- Otherwise, per unhealthy endpoint: URL, status code or curl error, response
  time, and a suggestion to check that service's logs.

## With /loop

```text
/loop 5m /deploy-health-watch
```

Run for 30 to 60 minutes after a deploy to catch delayed failures, then stop the
loop.
