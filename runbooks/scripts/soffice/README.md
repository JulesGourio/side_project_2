# soffice

One-shot scripts, run by hand and never deployed: nothing in the app or in a bundle job imports or runs them. They build the portable LibreOffice archive used by the "Exact (PDF)" preview, and check which shared libraries a LibreOffice tree needs.
## Files

| File | Kind | What it does |
|---|---|---|
| `package_libreoffice.py` | CLI | Turns the official LibreOffice Linux x86-64 deb bundle into one `tar.gz`, with the system libraries it needs merged into `program/` |
| `scan_needed_libs.py` | CLI | Lists the shared libraries an extracted LibreOffice tree needs but does not ship, excluding the libraries any glibc-based image provides |

## How to run

Run from the repository root:

```
python runbooks/scripts/soffice/package_libreoffice.py <downloaded.tar.gz> <output.tar.gz>
databricks fs cp <output.tar.gz> dbfs:/Volumes/<catalog>/<schema>/<volume>/libreoffice/libreoffice-linux-x64.tar.gz
python runbooks/scripts/soffice/scan_needed_libs.py <extracted_tree_dir>
```

The second line uploads the archive. The app reads the uploaded file through `SOFFICE_ARCHIVE_VOLUME_PATH=/Volumes/<...>/libreoffice/libreoffice-linux-x64.tar.gz`.

## Inputs and outputs

- `package_libreoffice.py` input: the TDF bundle `LibreOffice_<ver>_Linux_x86-64_deb.tar.gz`, downloaded beforehand. It must contain exactly one `opt/libreoffice*` tree and `program/soffice`.
- `package_libreoffice.py` output: `<output.tar.gz>`, one top-level folder holding the LibreOffice tree, with the system libraries of the Ubuntu jammy packages merged into its `program/` folder.
- `scan_needed_libs.py` input: an extracted tree, read through its ELF files. Output: the count of ELF files and of distinct needed sonames, then the sonames that are missing, one per line, on the console.

## Before you run

- Python 3 with the standard library only: both scripts import nothing else.
- Network access to `http://archive.ubuntu.com/ubuntu` (the jammy and jammy-updates package indexes and the packages). The bundle is not downloaded by the script.
- The Databricks CLI is on the PATH, and the target Unity Catalog volume exists, for the upload line.
