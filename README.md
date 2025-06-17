# Fitness Tracker

An open-source application for tracking heart-rate data from Bluetooth Low Energy (BLE) devices in real time, recording sessions, and visualizing your workout history.

## Features

* **Live Heart-Rate Monitoring**: Stream BPM, RR intervals, and energy (kJ) from supported BLE chest straps (e.g., Polar).
* **Session Recording**: Start and stop recording sessions; data is stored locally in SQLite.
* **History & Visualization**: Browse past activities by day, week, month, or all time with summary stats (duration, avg/max BPM) and sparkline previews.
* **Two-Way Sync**: Push local sessions to a remote database and pull remote sessions back to your local store.
* **Configurable**: Set custom database DSN, select your BLE device.
* **Dark & Light Mode**: Automatic theme adaptation based on your system preferences.

## Prerequisites

* BLE adapter (Bluetooth Low Energy support)

## Supported Devices

* Polar H10 (Supported by [BleakHeart](https://github.com/fsmeraldi/bleakheart))

## Configuration

1. **Set Database DSN**: In Settings, enter your remote database DSN (e.g., PostgreSQL URL) to enable syncing.
2. **Select BLE Device**: Choose your heart-rate monitor and save settings.

## Usage

* **Start Recording**: Click **Start** to begin a new session.

* **Stop Recording**: Click **Stop** to end the session and save data.

* **Sync Data**: Click **Sync to Server** to push/pull sessions to/from the remote database.

* **View History**: Switch to the **History** tab, filter by Last 7 Days, This Month, or All Time. Tick sessions to overlay detailed plots.

## Screenshots

![Tracker](docs/screenshots/tracker_page.png)
![History](docs/screenshots/history_page.png)

## Contributing

Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add awesome feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request
