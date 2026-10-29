# Deploying the Armature server behind a reverse proxy

`armature server run` listens on loopback by default (`host: 127.0.0.1`,
`port: 8098`). Put a TLS-terminating reverse proxy on the same host in front of
it, and set `public_origin` to the URL people type, such as
`https://armature.example.com`.

## Client addresses

Sign-in throttles count failures per client address. Behind a proxy, every
request arrives from `127.0.0.1`, so without the real address one attacker's
failures lock out everyone.

The server reads the `X-Real-IP` header only when both of these hold:

- `trust_real_ip: true` is set in `server.yaml`.
- The request comes from a loopback address, which is the proxy on this host.

It never reads `X-Forwarded-For`. Set `trust_real_ip` only when a proxy on this
host always overwrites `X-Real-IP`, because otherwise any client can choose its
own address and slip past the throttle.

### nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Caddy

```caddy
armature.example.com {
    reverse_proxy 127.0.0.1:8098 {
        header_up X-Real-IP {remote_host}
    }
}
```

### Apache httpd

```apache
ProxyPass        / http://127.0.0.1:8098/
ProxyPassReverse / http://127.0.0.1:8098/
RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"
```

Each of these overwrites any `X-Real-IP` the client sent.

## A base path

To serve the server under a path such as `/armature`, set
`base_path: /armature` and proxy that path without stripping it:

```nginx
location /armature/ {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Headers the server sets

Every response carries a strict Content-Security-Policy, `X-Frame-Options:
DENY`, and `Cache-Control: no-store`. Unless `public_origin` is a plain-HTTP
localhost address, it also sends `Strict-Transport-Security:
max-age=31536000`, so serve the origin over HTTPS only.

## Streaming runs from a host

A host that runs workflows with `armature run`, such as an armature-ops
machine, streams each run to the server when these environment variables are
set:

- `ARMATURE_SERVER`: the server's origin, such as
  `https://armature.example.com`. Plain HTTP works only to a loopback address.
- `ARMATURE_RUNNER_KEY`: an `ark_` runner key. A team lead creates a team key
  on the Runner keys tab of the team's page, and its runs land in that team. An
  admin creates an instance key on the Runner keys page, and each of its runs
  lands in the team whose project matches the run's repository.
- `ARMATURE_PROJECT`: the project the run belongs to. With a team key, this
  is a project name or repository in that team. With an instance key, this is
  a repository slug, such as `fs-eng/android-tree`, matched without regard to
  case, or `team-slug/project-name`. When it's unset, the run's `project`
  input is used, then its `repo` input. An instance key's run with no
  matching project lands in the admin-only "Operations (unassigned runs)"
  team.
- `ARMATURE_ACTOR`: who started the run. The default is `USER`.
- `ARMATURE_TRIGGER_KIND`: what started the run, such as `webhook` or
  `schedule`. The default is `cli`.

Deleting a runner key stops new registrations with it and ends the event
streams of the runs it registered.
