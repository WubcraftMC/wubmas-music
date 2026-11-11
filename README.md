# Wubmas music builder

This repository turns the DJ sets into the music packs for the Wubmas server. You need nothing on your own computer: you drag the sets into GitHub, press one button, wait, and run one command on the server. The encoding happens on GitHub's computers, never on the game server.

**What you do, every time:**

1. Put the sets in the draft release (below).
2. Run the workflow (Actions tab, "Build Wubmas music", Run workflow). It takes 10 to 20 minutes.
3. On the server: `/wubmusic update`

## One-time setup

1. Make this repository **public** (a private one will not do: players' games have to download the Java pack from it). The DJs' original files stay private anyway, see "Where the files are".
2. Put everything from this folder into the repository: Add file, Upload files, and drag in the *contents* of the unzipped folder, including the `.github` folder, then Commit.
   If the `.github` folder will not drag in, make the file by hand: Add file, Create new file, type `.github/workflows/build-music.yml` as the name, paste in the contents of that file, and Commit.
3. Open the Actions tab. If GitHub asks, click the green button that turns Actions on.
4. On the server, once: in `plugins/WubmasMusic/config.yml` fill in `library:` `url:` with the link the first successful build prints (see "On the server"), then run `/wubmusic reload`.

## Putting the sets in (the draft release)

GitHub's website only takes files up to 25 MB when you drop them into the repository itself, and a half-hour mp3 is bigger than that. A release takes files up to 2 GB, and a **draft** release is visible only to you and the other people who can push to this repository.

1. Releases, then **Draft a new release**.
2. Release title: **Wubmas music files** (exactly like that; the builder looks for this name). For the tag, type any name, for example `dj-files`. It does not matter, because this release is never published.
3. Drag the DJ files into the box that says to attach binaries. Name each file after the set it is (the ids are in `lineup.json`):

   | file | DJ |
   |---|---|
   | `dxbuddha.mp3` | DxBuddha |
   | `yungsiete.mp3` | Yung Siete |
   | `gnarlie.mp3` | Gnarlie |
   | `frgmntd.mp3` | FRGMNTD |
   | `voydoom_moros.mp3` | Voydoom B2B Moros |
   | `infrno.mp3` | INFRNO |
   | `jamesv.mp3` | James V |

   mp3, wav, flac, ogg and m4a all work, and the file name before the dot is what counts. Wait until every file's bar is full.
4. Click **Save draft**. Not "Publish release": that would make the files public.

To swap a file later, open the draft (Releases, the one marked Draft, Edit), click the cross next to the old file, drag the new one in, and Save draft again. You can add sets one at a time; a DJ who has no file yet stays in the lineup and the server tells you that set has no audio.

## Building

Actions tab, **Build Wubmas music** on the left, **Run workflow** on the right, and press the green button. 64 is a good quality for a game (about 100 MB for seven half-hour sets); 96 sounds a little better and makes a bigger download for every player.

When the run has a green tick, open it. The summary at the bottom shows the **link** for `library.url` and the report: each set's length and loudness, the size of the packs, and warnings (for example a set that is longer than its slot, which would be cut off when the next DJ starts). A red cross means it stopped; open the red step and read the last lines. The messages say what is wrong (a file named wrongly, no draft release found, and so on).

## On the server

`/wubmusic update` downloads the newest build and switches the server to it. It refuses while a set is playing. It also puts the Bedrock pack into Geyser's `packs` folder.

- **Java players** get the new pack when they next join, or when they type `/music pack`.
- **Bedrock players** get it from Geyser, which reads its packs when the server starts, so restart the server once after an update.
- `/wubmusic status` shows which build is in use.

## Changing names or times

Edit `lineup.json` here on GitHub (the pencil icon), then Commit changes. That starts a build by itself. After it finishes, run `/wubmusic update`.

## Where the files are

- The DJs' original files live in the draft release, which only people with push access to this repository can see.
- The finished packs are **public**: anyone with the link can download them, because players' games have to. They hold the music at game quality. Delete the releases after the festival (Releases, open one, Delete).
- The builder keeps the newest three builds and deletes older ones.

## What is in here

| path | what it is |
|---|---|
| `lineup.json` | the sets: id, name and start time |
| `.github/workflows/build-music.yml` | the build, run by the button |
| `scripts/` | download the sets, publish the release |
| `tool/` | cuts the sets into pieces and makes the Java and Bedrock packs |
