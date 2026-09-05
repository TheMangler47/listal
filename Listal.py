import argparse
import http.cookiejar
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait

import bs4

try:
    import lxml
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

ROOT = "https://www.listal.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
FULL_RE = re.compile(r"https://i\w+\.lisimg\.com/image/\d+/\d+full-[^'\"\s<>]+", re.I)
ANYIMG_RE = re.compile(r"https://i\w+\.lisimg\.com/image/\d+/[^'\"\s<>]+", re.I)
VIEWID_RE = re.compile(r"listal\.com/viewimage/(\d+)")
RELID_RE = re.compile(r'"/viewimage/(\d+)')
PAGER_RE = re.compile(r"pictures/+(\d+)\b")
EXTMAP = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

args = None
urls = None
profile_root = None
name = None
list_name = None
dest_dir = ""
started = time.time()


def fetch(url, data=None, timeout=30, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() == 200:
                    return resp.read()
        except urllib.error.HTTPError as herr:
            if herr.code == 404:
                return None
        except Exception:
            pass
        time.sleep(min(2 * attempt, 5))
    return None


def download_file(url, dest_path, timeout=60, retries=4):
    headers = dict(HEADERS)
    headers["Referer"] = ROOT + "/"
    headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() == 200:
                    data = resp.read()
                    if data:
                        if not os.path.splitext(dest_path)[1]:
                            ctype = resp.headers.get_content_type()
                            dest_path += EXTMAP.get(ctype, ".jpg")
                        with open(dest_path, "wb") as f:
                            f.write(data)
                        return True
        except urllib.error.HTTPError as herr:
            if herr.code == 404:
                return False
        except Exception:
            pass
        time.sleep(min(2 * attempt, 5))
    return False


def mksoup(url):
    raw = fetch(url)
    return bs4.BeautifulSoup(raw, PARSER) if raw else None


def find_image(html_text):
    m = FULL_RE.search(html_text)
    if m:
        return m.group(0)
    soup = bs4.BeautifulSoup(html_text, PARSER)
    img = soup.find("img", class_="pure-img")
    if img is not None:
        src = img.get("src") or img.get("data-src")
        if src:
            return src
    m = ANYIMG_RE.search(html_text)
    if m:
        return m.group(0)
    og = soup.find("meta", property="og:image")
    if og is not None and og.get("content"):
        return og["content"]
    return None


def safe_filename(s):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    return s or "listal"


def page_url(page_no):
    if page_no == 1:
        return profile_root + "/pictures"
    return "{}/pictures/{}".format(profile_root, page_no)


def crawl_page(page_no):
    raw = fetch(page_url(page_no))
    if raw is None:
        return page_no, set(), set()
    html = raw.decode("utf-8", "ignore")
    ids = {int(m.group(1)) for m in VIEWID_RE.finditer(html)}
    if not ids:
        ids = {int(m.group(1)) for m in RELID_RE.finditer(html)}
    higher = {p for p in (int(m.group(1)) for m in PAGER_RE.finditer(html)) if p > page_no}
    return page_no, ids, higher


def discover_images():
    all_ids = set()
    scheduled = set()
    pages_done = 0
    with ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = set()

        def consider(page_no):
            if (args.last_page is None or page_no <= args.last_page) and page_no not in scheduled:
                scheduled.add(page_no)
                futures.add(executor.submit(crawl_page, page_no))

        consider(args.first_page)
        while futures:
            finished, futures = wait(futures, return_when=FIRST_COMPLETED)
            for fut in finished:
                page_no, ids, higher = fut.result()
                new = len(ids - all_ids)
                all_ids.update(ids)
                pages_done += 1
                for p in sorted(higher):
                    consider(p)
                if new and not higher:
                    consider(page_no + 1)
            sys.stdout.write("\r Pages fetched: {} | Images found: {} ".format(
                pages_done, len(all_ids)))
            sys.stdout.flush()
    sys.stdout.write("\n")
    return all_ids


def get_list_ids(url):
    soup = mksoup(url)
    if soup is None:
        sys.exit("ERROR: could not fetch the list page.")
    cl = soup.find(id="customlistitems")
    if cl is None:
        sys.exit("ERROR: page does not look like a listal list (no #customlistitems).")
    if cl.get("data-listformat") != "images":
        sys.exit("This is not an Image list. Currently only Image lists are supported.")
    list_id = int(cl.get("data-listid"))
    header = soup.find("div", "headertitle")
    title = header.text.strip() if header else urls.path[6:].replace("-", " ").title()
    first_div = cl.find("div")
    total = int(first_div["data-itemtotal"]) \
        if first_div is not None and first_div.get("data-itemtotal") else 0
    ids = set()
    for each in soup.find_all("div", "imagelistbox"):
        a = each.find("a")
        if a is not None and a.get("href"):
            try:
                ids.add(int(a["href"].strip().split("/")[-1]))
            except ValueError:
                pass
    lm = soup.find("div", "loadmoreitems")
    offset = int(lm["data-offset"]) if lm is not None and lm.get("data-offset") else len(ids)
    misses = 0
    while (offset < total if total else True) and misses < 3:
        data = urllib.parse.urlencode({"listid": list_id, "offset": offset}).encode()
        raw = fetch(ROOT + "/item-list/", data=data)
        if raw is None:
            misses += 1
            continue
        found = {int(x) for x in re.findall(r"viewimage\\?/(\d+)", raw.decode("utf-8", "ignore"))}
        if not found:
            misses += 1
            continue
        misses = 0
        ids.update(found)
        offset += len(found)
    if misses >= 3:
        print("WARNING: server stopped returning new items; continuing with what we have.")
    return title, ids


def download_one(img_id):
    raw = fetch("{}/viewimage/{}h".format(ROOT, img_id), timeout=25)
    src = find_image(raw.decode("utf-8", "ignore")) if raw is not None else None
    if src is None:
        return img_id, "fail"
    basename = os.path.basename(urllib.parse.urlparse(src).path) or "image"
    basename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", basename)
    path = os.path.join(dest_dir, "{}_{}".format(img_id, basename))
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return img_id, "skip"
    return img_id, ("ok" if download_file(src, path) else "fail")


def update_progress(total, done, counts):
    progress = int(100 * done / total) if total else 100
    sys.stdout.write("\r {:0>3}% [{:<50}] ({}/{})  ok:{} skip:{} fail:{} ".format(
        progress, "#" * (progress // 2), done, total,
        counts["ok"], counts["skip"], counts["fail"]))
    sys.stdout.flush()


def main():
    global args, urls, profile_root, name, list_name, dest_dir
    parser = argparse.ArgumentParser(
        description="Enter a listal.com link (profile or list) and download all its pictures.")
    parser.add_argument("url", nargs="?", default=None,
                        help="listal.com link (or just the profile name). Omit to be prompted.")
    parser.add_argument("--from", dest="first_page", type=int, default=1,
                        help="Profile page no. to start from (default: 1).")
    parser.add_argument("--upto", dest="last_page", type=int, default=None,
                        help="Only scrape upto this profile page no.")
    parser.add_argument("--threads", dest="threads", type=int, default=10,
                        help="No. of threads (default: 10).")
    parser.add_argument("-o", "--out", dest="out_dir", default=None,
                        help="Folder to save pictures into (default: profile/list name).")
    args = parser.parse_args()
    args.threads = max(1, args.threads)
    if args.first_page < 1:
        args.first_page = 1
    if args.last_page is not None and args.last_page < args.first_page:
        sys.exit("--upto must be >= --from")

    if not args.url:
        args.url = input("Enter a listal.com link (profile or list): ")
    url_in = args.url.strip().strip('"').strip("'")
    if "listal.com" not in url_in.lower():
        url_in = "https://www.listal.com/" + url_in.strip("/")
    elif "://" not in url_in:
        url_in = "https://" + url_in
    tmp = urllib.parse.urlparse(url_in)
    if tmp.netloc.lower() not in ("www.listal.com", "listal.com"):
        sys.exit("That does not look like a listal.com link.")
    urls = urllib.parse.urlparse("https://" + tmp.netloc.lower() + tmp.path)

    print("Scraping :", urls.geturl())

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    urllib.request.install_opener(opener)

    is_list = urls.path.startswith("/list/")
    if is_list:
        if args.first_page != 1:
            print("Entered URL is of a list. The '--from' option is ignored.")
        if args.last_page is not None:
            print("Entered URL is of a list. The '--upto' option is ignored.")
        list_name, all_ids = get_list_ids(urls.geturl())
    else:
        profile_root = urls.geturl().split("/picture")[0].rstrip("/")
        name = urllib.parse.urlparse(profile_root).path[1:].replace("-", " ").title()
        all_ids = discover_images()

    print("Found {} unique images.".format(len(all_ids)))
    print("Time Taken :", time.strftime("%H:%M:%S", time.gmtime(time.time() - started)))

    if not all_ids:
        print("No images found. Nothing to download.")
        return

    base = list_name if is_list else name
    dest_dir = args.out_dir or safe_filename(base)
    os.makedirs(dest_dir, exist_ok=True)

    print("Downloading {} pictures to '{}' ...".format(len(all_ids), dest_dir))
    total = len(all_ids)
    counts = {"ok": 0, "skip": 0, "fail": 0}
    failed = []
    done = 0
    executor = ThreadPoolExecutor(max_workers=args.threads)
    try:
        futures = [executor.submit(download_one, i) for i in sorted(all_ids)]
        for f in as_completed(futures):
            img_id, status = f.result()
            counts[status] += 1
            if status == "fail":
                failed.append(img_id)
            done += 1
            update_progress(total, done, counts)
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        print("\nInterrupted. Pictures already downloaded stay in the folder.")
        sys.exit(130)
    executor.shutdown(wait=True)

    sys.stdout.write("\n")
    print("Done : {} downloaded, {} already existed, {} failed.".format(
        counts["ok"], counts["skip"], counts["fail"]))
    if failed:
        print("Failed ids:", ", ".join(map(str, failed[:30])),
              "..." if len(failed) > 30 else "")
    print("Saved in :", os.path.abspath(dest_dir))
    print("Time Taken :", time.strftime("%H:%M:%S", time.gmtime(time.time() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)

