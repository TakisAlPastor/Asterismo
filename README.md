# Asterismo

**Asterismo** is an upcoming, unified audio research tool for Windows. It brings together the whole process of grabbing media, building fingerprint databases, and identifying lostwaves into one single desktop app.

1. **Archive Harvesting & Ingestion**: Downloading YouTube channels, sound archives, and mix-tapes, chopping up long audio files, and compiling `.pklz` landmark databases.
2. **Song Identification & Acoustic Matching**: Running unknown snippets against local fingerprint collections as well as online recognition services, often trying out different pitch and speed tweaks to catch distorted or analog recordings.

**Asterismo** aims to roll this entire workflow into a clean, modern, all-in-one app.

## Functional Inspiration & Credits

Asterismo is conceptually and functionally inspired by two foundational tools developed by the lostwave community:

- [**WerZatSonGUI**](https://github.com/LostwaveItalia/WerZatSonGUI): For establishing the multi-engine desktop workflow (Audfprint, Shazam, AcoustID, AudioTag) and tempo/pitch shift analysis.
- [**Fingerprinter**](https://github.com/EierkuchenHD/fingerprinter): For pioneering automated batch downloading via `yt-dlp`, audio segmentation, and `.pklz` acoustic database compilation.
- [**audfprint**](https://github.com/dpwe/audfprint) (and the [WerZatSong fork](https://github.com/Nel80s/WerZatSong/tree/main/libs/audfprint)): The landmark-based acoustic fingerprinting algorithm powering local lostwave databases.

*(Note: Asterismo is an independent reimplementation built from scratch; it does not reuse the codebases of these tools).*

## 📄 License

This project is licensed under the [MIT License](LICENSE).
