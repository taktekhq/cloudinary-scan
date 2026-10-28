# cloudinary-scan

Find websites that serve their images from [Cloudinary](https://cloudinary.com), starting from a ranked list of domains such as the [Tranco](https://tranco-list.eu) top 1 million.

It fetches each site's home page once and looks for Cloudinary delivery URLs. One JSON line per match, nothing else is stored. Python 3.9+ standard library, plus `curl`.

## What it detects

| Pattern | Example | Meaning |
|---|---|---|
| Shared host | `res.cloudinary.com/<cloud>/image/upload/...` | Any plan |
| Private CDN | `<cloud>-res.cloudinary.com/image/upload/...` | Advanced plan and up |
| Custom domain | `img.example.com/image/upload/w_400,c_fill/...` | Advanced plan and up. A guess: it only counts when the path has transformation segments |

It also reads the transformations in each URL, so you can see whether a site already uses `f_auto` and `q_auto`.

## Run it locally

```sh
curl -L -o top-1m.csv.zip https://tranco-list.eu/top-1m.csv.zip && unzip top-1m.csv.zip
python3 cloudinary_scan.py --list top-1m.csv --count 10000 --out scan.jsonl
```

The list is `rank,domain` per line. Useful flags:

| Flag | Default | What it does |
|---|---|---|
| `--start N` | 0 | Skip the first N rows. `--start 100000` begins at rank 100,001 |
| `--count N` | 10000 | Rows to scan |
| `--workers N` | 96 | Parallel fetches |
| `--timeout S` | 12 | Seconds per request |
| `--measure N` | 0 | Also send a HEAD request for up to N Cloudinary images per matching site, and estimate bytes per page view |
| `--resume` | off | Skip domains already in `--out` |
| `--user-agent UA` | see below | User agent to send |

Output fields per site: `rank`, `domain`, `final_url`, `clouds` (cloud name to reference count), `private_cdn`, `custom_hosts`, `refs`, `unique_urls`, `transforms`, `f_auto`, `q_auto`, `top_transforms`, `sample`, and with `--measure`: `measured`, `avg_bytes`, `page_image_bytes`.

Run the tests with `python3 -m unittest`.

### Be a good neighbour

- It makes one GET for the home page (a second for the `www.` form if the first fails), and with `--measure` a few HEAD requests for images. It does not crawl further and does not read `robots.txt`.
- Set your own user agent so site owners can reach you: `--user-agent "MyScan/1.0 (+https://example.com/about-this-scan)"` or the `CLOUDINARY_SCAN_UA` environment variable. The default points to this repo.
- Home page HTML only. Images added by JavaScript after load are missed, so counts are a floor.

## Run it on GitHub Actions

A scan of the top 1 million takes about 90 shards of 10,000 sites, each 12 to 50 minutes. Public repositories get free Actions minutes, so fork this repo or use it as a template and run it there.

Because the repo is public, anyone with a GitHub account can download its artifacts and read its run logs. The workflow therefore does two things:

1. The logs show counts only, never domains.
2. Each shard's result is encrypted with [age](https://age-encryption.org) before it is uploaded. Only the `.age` file leaves the runner.

If the repository variable `AGE_RECIPIENT` is not set, the workflow stops before scanning. It never uploads plaintext.

### One-time setup

```sh
# 1. Make a key pair. Keep the private key file out of git and out of the cloud.
mkdir -p ~/.config/cloudinary-scan && chmod 700 ~/.config/cloudinary-scan
age-keygen -o ~/.config/cloudinary-scan/age.key
chmod 600 ~/.config/cloudinary-scan/age.key

# 2. Give the repo only the PUBLIC key.
gh variable set AGE_RECIPIENT --body "$(age-keygen -y ~/.config/cloudinary-scan/age.key)"

# 3. Optional: your own user agent for the runs.
gh variable set SCAN_USER_AGENT --body "MyScan/1.0 (+https://example.com/about-this-scan)"
```

Install age with `apt install age`, `brew install age`, or from the [releases page](https://github.com/FiloSottile/age/releases).

### Start a run

```sh
# ranks 100,001 to 1,000,000: skip 100000 rows, 90 shards of 10000
gh workflow run scan.yml -f start=100000 -f shard_size=10000 -f shards=90 -f measure=20
gh run list --workflow scan.yml
gh run watch <run-id>
```

| Input | Default | Meaning |
|---|---|---|
| `start` | 0 | Rows to skip in the list |
| `shard_size` | 10000 | Rows per shard |
| `shards` | 10 | Number of parallel jobs (1 to 256, the GitHub matrix limit) |
| `measure` | 20 | Images to HEAD per matching site (0 turns it off) |
| `list_url` | Tranco top 1M | Any zip or plain CSV of `rank,domain` lines |

The list is downloaded once by a first job and shared, so every shard scans the same snapshot even if Tranco publishes a new list mid-run. Artifacts are kept for 7 days.

### Download and decrypt

```sh
python3 collect.py <run-id> --repo OWNER/REPO \
  --identity ~/.config/cloudinary-scan/age.key --out all.jsonl
```

`collect.py` downloads every `scan-*` artifact with `gh`, decrypts each in memory with `age`, and writes one file sorted by rank, one row per domain (mode 600). To decrypt a single shard by hand:

```sh
age -d -i ~/.config/cloudinary-scan/age.key scan-0.jsonl.age > scan-0.jsonl
```

Lose the private key and the results are gone. Back it up somewhere private.

## License

MIT. Copyright (c) 2026 Taktek and Nizar Mahmoud.
