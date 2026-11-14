# perf-dashboard

Performance testing and performance CI for Collabora Online.

Two layers live here:

- a k6 based test suite that drives a real browser against a Collabora Online
  server, with an embedded WOPI host so nothing external is needed. This half
  comes from cool-k6 (https://github.com/hfiguiere/cool-k6), kept as the
  `upstream` remote.
- a performance CI on top of it: repeated runs over a corpus of documents,
  per document flamegraphs and perf counters, and a dashboard built from the
  run history so a regression is visible as a step in a chart.

Start with `PERFORMANCE_TESTING.md` to run a test by hand, and with
`CI-JENKINS.md` to run the suite from Jenkins on a dedicated VM.
`DEPLOY-VM.md` sets up a VM that builds main and measures it every night
with a systemd timer, without Jenkins.

## The performance suite

| Tool | What it does |
| --- | --- |
| `browser-suite.py` | runs the browser tests over a document corpus, records each run under `results/`, and builds `browser-dashboard.html` from the whole history |
| `templates/` | the dashboard pages that `browser-suite.py` and `plot-metrics.py --html` fill: their html, css and script |
| `cool-ci` | the Jenkins entry point. Derives the coolwsd command line from the target tree instead of running make, so a job measures the build it was pointed at |
| `cool-use-build` | installs a kept-aside build into an Online tree, and prints that coolwsd command line |
| `k6-flamegraph.py` | one profiled run: starts the WOPI host and coolwsd, drives a k6 test, and renders a flamegraph scoped to the coolwsd and kit processes |
| `cool-perf` | the same capture for an ad-hoc window against an already running server, including one in a container |
| `plot-metrics.py` | charts from a single run's metrics |

`cool-ci doctor` self-checks everything a run needs, and every tool takes
`--help`.

`npm run format` formats the pages in `templates/` with Prettier, and
`npm run format:check` checks them.

## Test documents

The corpus is generated, not committed: `documents/gen-docs.py` builds the
~5 MB Writer, Calc, Impress and Draw documents, then converts them to `.docx`,
`.xlsx` and `.pptx`, so a format comparison measures the format rather than two
different documents. For Impress it also builds `impress-duplicate.odp`, the
deck the slide-duplication requirement is measured on. The small `blank.od*`
files are committed since they are a few kilobytes each.

```shell
python3 -m pip install -r documents/requirements.txt
./documents/gen-docs.py
./documents/gen-docs.py --apps writer --mb 10 --report --no-ooxml
```

`gen-docs.py` also writes one standalone image of the same size,
`image-5mb.png`, which the insert-image test inserts into an open document.
`--no-image` skips it.

The size names the document, so the second line writes `writer-10mb.odt`
beside the 5 MB one rather than over it. The conversion needs a system
LibreOffice, and `--no-ooxml` skips it. An Online engine build refuses
`--convert-to`.

`--report` decides what the size is made of, and the 10 MB document needs it.
The corpus documents pad to their size with images of random noise, which no
encoder can shrink, so 10 MB arrives in 7 images and about ten pages. A report
pads with photographs, which compress the way photographs do, so the same
10 MB is 46 sections over 47 pages, each with a table and a photograph, behind
a table of contents that lists all 46. That is the document the requirement
below is written about, and it puts the load time where a real document puts
it, in the layout rather than in seven bitmaps.

### Reference documents

The generated documents have a known shape and grow by images. Real documents
are hard on the applications in other ways: the layout of hundreds of pages,
millions of cells and formulas, dense tables over many sheets, animated
images. `documents/reference-docs.json` lists ten of them, with their source
and checksum, and `documents/fetch-reference-docs.py` downloads them into
`documents/ref/`:

| Test | Document |
| --- | --- |
| `writer-rtl-book` | a commentary in Persian: 960 pages of right-to-left text in 910000 runs |
| `writer-odf-spec` | OpenDocument 1.2 Part 1: 846 pages of cross-references |
| `writer-guide` | LibreOffice Getting Started Guide: 531 pages, 659 images, tracked changes |
| `writer-long-table` | one table over 268 pages with 118 images |
| `calc-formulas` | 6 million cells, 500000 of them formulas |
| `calc-matrix` | 31 sheets of dense tables, slow to open |
| `calc-countifs` | 1.9 million cells under COUNTIFS |
| `impress-gifs` | 18 slides with 19 MB of animated GIFs |
| `impress-yottadb` | 29 slides with 15 MB of images |
| `impress-candy` | 104 slides and 5969 shapes |

The OpenDocument standard comes from OASIS. The others are attachments of
open performance bugs of LibreOffice, which their reporters left as examples
of what was slow for them; they carry no license of their own, so they are
downloaded rather than committed. A file whose checksum changed fails the
download, so that a test never measures another document under the same name.

```shell
./documents/fetch-reference-docs.py --list
./documents/fetch-reference-docs.py
./browser-suite.py --run --documents reference --users 1,3
```

`--documents reference` runs them alone and `--documents all` after the
generated ones. They take up to a minute to load, so the default run leaves
them out. With 3 users, each user types a word and Enter at the cursor in the
Writer and Calc documents, rather than making the whole document bold.

## The service-level requirements

Three tests assert a service level rather than only recording numbers.

### Opening a 10 MB document

> After clicking on a 10 MB Office document in the cloud storage, editing in
> the WebOffice web client must be possible for at least 98% of all users
> after a maximum of 5 seconds.

`src/cool-browser-writer-10mb-test.js` opens `documents/writer-10mb.odt`, the
report described above, and holds the 98th percentile of `document_loaded_ms`
to 5000 ms. That is the editor reporting `App_LoadingStatus: Document_Loaded`,
the moment the document is open and editable.

`Frame_Ready`, which the other tests report, is the client shell coming up and
says nothing about the document: measured here, a blank document reaches it in
1258 ms and the 46-page report in 1134 ms, while `Document_Loaded` separates
them properly at 1828 ms and 2666 ms. Every test records both, and the charts
plot both.

The budget
is declared once in the test and is enforced twice: as a k6 threshold, so k6
exits non-zero by itself, and as a verdict in that run's `metrics.json`, which
`browser-suite.py` fails the run on and reports through `--junit`.

A 98th percentile needs at least 50 opens to be one; below that it is simply
the slowest open of the run. The suite's own 1u and 3u passes therefore report
the requirement over a handful of opens, and the run that answers it properly
is an explicit one:

```shell
USERS=10 ITERATIONS=100 MAXDUR=30m ./k6-run dist/cool-browser-writer-10mb-test.js
```

### Inserting a 5 MB image

> After inserting a 5 MB image from the local file system into a text document
> that has already been fully loaded in WebOffice, the inserted image must be
> displayed in the WebOffice document within a maximum of 2 seconds for at
> least 98% of all users.

`src/cool-browser-writer-image5mb-test.js` opens `documents/blank.odt`, waits
until it is loaded and the page has stopped changing, inserts
`documents/image-5mb.png` through the file input behind Insert > Image, and
holds the 98th percentile of `image_displayed_ms` to 2000 ms.

The image is read from the local file system by the test and handed to that
input, which is the path a person takes through the file picker, so the number
covers what a person waits for: the upload of the file, the insert in core, and
the tile that comes back. The clock runs from the input's `change` event, the
moment the client holds the picked file, to the first animation frame in which
the document canvas shows it, both taken in the page.

This test opens the document directly rather than through the WOPI host page,
because it reads the client's own page: the file input and the document canvas.
Nested in the host page the client is a frame of another origin, where neither
is reachable. The client posts no `App_LoadingStatus` to a host it does not
have, so this test's `document_loaded_ms` is the document being open and still
rather than the `Document_Loaded` message the other tests take it from.

The same percentile arithmetic applies, so the run that answers it properly is
again an explicit one:

```shell
USERS=10 ITERATIONS=100 MAXDUR=30m ./k6-run dist/cool-browser-writer-image5mb-test.js
```

`IMAGE` names another file to insert and `FILE_ID` another document to insert
it into, both relative to the WOPI host's table in `server/routes/files.json`.

### Duplicating a slide

> Duplicating a slide (formatted text, a photo, five drawing objects such as
> arrows, circles, rectangles) from a presentation file that has already been
> fully loaded into WebOffice is completed after 1 second for 98% of all
> users.

`src/cool-browser-impress-duplicate-test.js` opens
`documents/impress-duplicate.odp`, a deck whose every slide holds exactly
that, waits for the document to settle, duplicates the current slide
`DUPLICATES` times (default 5), and holds the 98th percentile of
`slide_duplicated_ms` to 1000 ms. Collabora Online sends no "done" for
`.uno:DuplicatePage`, so completion is read off the client, from inside its
frame: `slide_duplicated_ms` is the new slide painted on the main canvas, the
view switched to it and every visible tile of it drawn. That is the moment the
user sees the duplicate. The signals on the way there are recorded beside it:
`slide_confirmed_ms` (the server's status message with one more slide),
`slide_shown_ms` (the view switched), `slide_thumbnail_ms` (the slide sorter
shows its thumbnail, absent when the sorter does not show that slide) and
`slide_status_ms` (the status bar says so, which trails the rest by hundreds of
milliseconds because core coalesces state changes). `lib/duplicate_slide_probe.js`
documents each one.

The suite's multi-user pass has every user in the same document, so each
one's duplicates land in everyone's deck. The probe therefore follows the new
slide by identity, not by position, and the deck grows through a run. That is
why the count per open is kept small and the requirement run takes its samples
from many opens:

```shell
USERS=5 ITERATIONS=20 MAXDUR=30m ./k6-run dist/cool-browser-impress-duplicate-test.js
```

Measured here against a local build: 55 to 70 ms per duplicate with one user,
100 to 270 ms with three users in the same deck.

## Adding test

Add test to the `src/` directory. Files implementing tests must end in
`-test.js`.

Build the bundles with `npm run build`. The targets are in `dist/`
ready to run. This uses webpack under the hood and the JavaScript
dialect is ES6.

## WOPI Host

A WOPI Host (server) is included in the `server` directory. It's in
pure NodeJS. `npm run server` will start it bound to port 3000.

The browser tests need it on https, because the host page refuses to submit to
a Collabora Online of another scheme and the test then waits for a
`Frame_Ready` that never arrives. The certificate is not in the repository, so
generate one per checkout:

```shell
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout server/npm-server-key.pem -out server/npm-server-cert.pem \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

```shell
SSL_KEY_FILE=server/npm-server-key.pem SSL_CRT_FILE=server/npm-server-cert.pem \
  NODE_TLS_REJECT_UNAUTHORIZED=0 npm run server
```

`cool-ci` and `browser-suite.py` expect the pair under those two names and
start the host themselves.

Running the server require Node 20.10 or later.

### Configuring WOPI files

You can configure files to serve from the WOPI Host. This is done by
adding an entry to the JSON file `./server/routes/files.json`.

It's a dictionary (JS object) where the key is the file id as passed
in the WOPI source URL, and entries contain at least a `path`
(relative to the current working directory or absolute). So for
`https://WOPI_HOST/wopi/files/1` the entry is `"1"`.

There are the various fields for an entry:

- `path`: Path to the file to serve.
- `readonly`: If true the file is served in readonly mode. In absence
of the property, the file is server in editing mode.

A key with an empty object return just a "Hello world" strigs. Asking
a file with an ID that is not found in the `files.json` will return
the HTTP status 404 (not found).

Example:

```JSON
{
    "1": {
    },
    "2": {
        "path": "documents/writer-large.fodt",
        "readonly": false,
        "wopi": {
            "EnableInsertRemoteImage": true,
            "DisableInsertLocalImage": true,
            "HidePrintOption": true,
            "HideSaveOption": false
        }
    }
}
```

### Configuring static files

You can configure the server to serve static files from
`https://WOPI_HOST/static/`. This is done by adding an entry to the JSON
file `./server/routes/static_files.json`.

Static files are used for tests like "inserting an image" or any other
that would require an URL. It's like for WOPI files. The keys are the
file names. So for `https://WOPI_HOST/static/image.png`, the entry is
`"image.png"`.

Each entry need:
- `path`: Path to the file to serve.
- `type`: An optional MIME type. If missing `application/octet-stream`
  will be used.

Example:

```JSON
{
    "image.png": {
        "path": "documents/image.png",
        "type": "image/png"
    }
}
```

### Default values

By default the WOPI file server will set the following properties with
values:

- `UserId`: 1
- `UserCanWrite`: true
- `IsAdminUser`: false

## Running the tests

We default to use the Docker image for k6 since it's not carried by
most distributions. But you can adapt this to other methods.

This is currently tested with k6 version 1.7.1.

### Environment

There are two required environment variables used by the K6 tests.

- `WOPI_URL=https://<your_cool_host>:9980/`: `WOPI_URL` is set to the
WOPI client (Collabora Online) URL. `your_cool_host` is the host on
which Collabora Online is running. Remember if you run K6 in a Docker
container, it's unlikely to be localhost.

- `WOPI_HOST=https://<your_wopi_host>:3000/`: `WOPI_HOST` is set to
the WOPI host (see above). `your_wopi_host` is the host on which you
run the WOPI host started with `npm run server`. Remember if you run
K6 in a Docker container, it's unlikely to be localhost.

The test will use default values, unlikely to be what you want.

You can also optionnaly set the following:

- `COOL_K6_SCREENSHOT_DIR`: This  is set to the directory  in which to
write the screenshot  in case of failure in browser  tests. If running
K6 in Docker, then care should be taken to ensure the directory can be
written bu k6 user from inside docker. `k6-wrap` takes care of it.

### Running

```shell
npm run build
docker run -v $PWD:/app:Z \
       -w /app --rm -i \
       -e WOPI_URL=$WOPI_URL \
       -e WOPI_HOST=$WOPI_HOST \
       -e COOL_K6_SCREENSHOT_DIR=$COOL_K6_SCREENSHOT_DIR \
       grafana/k6:1.7.1-with-browser -v run --vus 1 \
       --summary-mode=compact \
       "$@"
```

`k6-wrap` is a convenient wrapper to run k6 using docker.

`k6-run` uses `k6-wrap` to perform the `run` command from k6. You can
use it as a base to learn how to use the wrapper for other purposes.

The tests are webpacked into the `dist/` directory. To run a test
using `k6-wrap`, use the following command:

```shell
./k6-run dist/cool-test.js
```

### Other options

`--insecure-skip-tls-verify` is necessary if you use self-signed
certificates. Make sure the Collabora Online server also accept
them. Or use proper certificates everywhere.

The `k6-run` wrapper will detect `NODE_TLS_REJECT_UNAUTHORIZED` as
used by Node. If it is setp to `0`, the script will add the above
options accordingly.

### Diagnostics

Existing browser tests will save a  screenshot of the page if the test
fail.  This  allow having  an overview  if the page  in an  attempt to
figure out  what is happening. Each  screenshot file name will  have a
timestamp.

## License

This code is licensed under [MPL-2.0](COPYING) license.

Contributing is subject to the [Code of Conduct](CODE_OF_CONDUCT.md).
