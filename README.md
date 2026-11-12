# Adhan Cast

Plays the adhan on your Google Cast speakers (Nest Mini, Nest Audio, Nest Hub, Chromecast, speaker groups) at each prayer time, with an optional iqama alert for each prayer. Runs on your Windows PC; you control it from a web dashboard.

## Setup (once)

1. Install **Python 3.10+** from python.org and tick **"Add python.exe to PATH"**.
2. Unzip this folder somewhere permanent, for example `C:\AdhanCast`.
3. Double-click **`start.bat`**. The first run installs packages (about a minute), then opens the dashboard at http://localhost:8080.
4. When **Windows Firewall** asks, allow Python on **Private networks**. Your speakers download the audio from this PC, so without this step nothing plays. Also make sure your Wi-Fi is set to *Private* (Settings → Network & internet → Wi-Fi → your network).
5. In the dashboard, open **General** and set your location (name, time zone, latitude and longitude). The default is Makkah. Then choose a **default speaker**, adjust each prayer, and click **Save changes**. Use **▶ Test** to confirm sound comes out.

## Run it in the background

Double-click **`install-startup.bat`**. It starts Adhan Cast with no window and adds it to your sign-in startup. Bookmark http://localhost:8080 to get to the dashboard.
To stop it and remove it from startup, run `install-startup.bat remove`.

## Sleep

The app keeps Windows from going to sleep on its own while it runs ("Keep this PC awake" in General). It **can't** stop a sleep you trigger yourself: closing a laptop lid, or Start → Sleep. If the PC was asleep at prayer time, the Activity log shows "Missed…". For a laptop, set *Lid close action* to "Do nothing" when plugged in (Control Panel → Power Options).

## Your own adhan audio

The built-in sounds are placeholder tones; no adhan recordings are included. Download an adhan recording you have the right to use, then click **Upload audio** in the dashboard (MP3, M4A, WAV, OGG or FLAC), then pick the file for each prayer. Fajr has its own sound setting, so you can use the Fajr adhan there.

## Masjid times (Masjidal)

No masjid is set up at first, so the app uses calculated ISNA times for your location. Once you add a masjid (see below), the app downloads 30 days of the masjid's adhan and iqama times from Masjidal, refreshes them daily, and stores them in `masjid_cache.json`. If a download fails, the stored times are used. If there are no stored times for the day, the app falls back to the ISNA calculation, and the dashboard marks it "calc".

To add another masjid, go to **Masjid → Add a masjid**. Paste the masjid's website, its Masjidal link, or its masjid ID, then click **Look up**. The app finds the masjid ID on the page and shows today's times for you to check. Click **Add & use**, then **Save changes**. This only works for masjids that publish their times through Masjidal.

On Fridays, the Dhuhr iqama alert uses Jumu'ah 1 by default. You can switch it to Jumu'ah 2 or to the regular Dhuhr iqama.

## Dashboard features

- Today's adhan and iqama times, plus a countdown to the next event.
- For each prayer: adhan on/off, sound, speaker or group, volume, and a ±30 minute shift.
- **Iqama alert** for each prayer. It can go off X minutes before or after the masjid's iqama (0 means exactly at the iqama), X minutes before or after the adhan, or at a set clock time. It has its own sound, speaker and volume.
- After playing, each speaker's volume is put back to where it was.
- Stop buttons, speaker rescan, audio upload and preview, and an activity log (also written to `adhan.log`).

## Troubleshooting

- **No speakers listed:** the PC must be on the same network as the speakers, not a guest network. Some routers block discovery between 2.4 and 5 GHz or between wired and Wi-Fi; click Rescan. Speakers appear a few seconds after start.
- **Speaker connects but stays silent:** this is almost always the firewall (step 4). If the PC has several network adapters (VPN, Hyper-V, WSL), type its Wi-Fi IP in *This PC's network address*. You can find it in Settings → Network & internet → Wi-Fi → Properties.
- **Port 8080 is in use:** set the environment variable `ADHAN_PORT` to another port.

Settings live in `settings.json` and uploaded audio in `sounds/`. Keep both if you move the folder.
