# maubot-reddit-preview

A [maubot](https://github.com/maubot/maubot) plugin that watches every room the bot has joined. When someone posts a Reddit link, the bot replies with a preview of that post: title, subreddit, author, score, comment count, body text and the top comments. It can also upload the post's images (including gallery and inline images) to the room.

It uses only libraries that come with maubot (`aiohttp`, `yarl`, `mautrix`, `commonmark`), so there is nothing to `pip install`.

## Supported links

- `reddit.com/r/<sub>/comments/<id>/...` (also `www.`, `old.`, `new.`, `np.` and other subdomains)
- Comment permalinks `.../comments/<id>/<slug>/<comment_id>`: the bot shows the linked comment.
- `redd.it/<id>` short links
- `reddit.com/r/<sub>/s/<code>` share links (the bot follows the redirect)

## Setup

1. Create a Reddit app at <https://www.reddit.com/prefs/apps> and choose the **script** type.
2. Upload the `.mbp` from the [latest release](../../releases) to maubot, then create an instance.
3. Fill in the instance config:

```yaml
reddit:
  client_id: ""      # string under the app name ("personal use script")
  client_secret: ""  # the app's "secret"
  username: ""       # Reddit account that owns the app
  password: ""
  user_agent: ""     # optional; auto-generated when empty
```

All other options (number of comments, body length, image uploads, NSFW handling, room allowlist, etc.) are explained in [`base-config.yaml`](base-config.yaml).

Without credentials, the bot tries Reddit's public JSON endpoints. Reddit usually blocks these with HTTP 403, so credentials are effectively required.

## Releases

Every push runs `.github/workflows/release.yml`. The workflow builds `<id>-v<version>.<run_number>.mbp` and attaches it to a new GitHub release:

- Pushes to the default branch create a normal release `v<version>.<run_number>`.
- Pushes to other branches create a pre-release tagged with the branch name.

The build number is added to the version so that each upload to maubot is a newer version. To change the base version, edit `version` in `maubot.yaml`.

## Building manually

```sh
zip -9r reddit-preview.mbp maubot.yaml base-config.yaml reddit_preview
```
