# Site image backup

A copy of every image the Locke Incorporated Wix site uses, so the site can leave Wix without losing them.

- **REPORT.md**: the latest results: how many images have a copy here, and which supplier links still work.
- **wix-manifest.csv**: one row per Wix image: its Wix link and the file in this repo that holds the same image. Images that were already in `photos/` are pointed to there instead of being stored twice. If the site ever moves off Wix, this file is the map for rewriting every image link.
- **wix/**: Wix images that weren't already in this repo.
- **supplier-image-links.csv**: images the site loads straight from supplier sites (Home Depot, Shopify stores, Clé, cabinet suppliers and others), including the product gallery photos. These are only checked, not copied, because they belong to those companies.

The job runs every Monday and can be run any time from the Actions tab ("Back up site images" → Run workflow). By default it works from `wix-media-ids.txt`. To have it pick up new images from the site code automatically, add a read-only GitHub token for `lockecorporation/locke-construction` as a repository secret named `SITE_REPO_TOKEN`.
