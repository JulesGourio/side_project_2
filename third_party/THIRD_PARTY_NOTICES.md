# Third-party notices

Third-party files shipped in this repository, with their licence. What the repository files do not state is marked
"to confirm". Python and JavaScript dependencies are listed in `requirements.txt` and `client/package.json`; their
licences are those of the packages.

## antiword

| | |
|---|---|
| Used for | Converting `.doc` files to text (`third_party/antiword/`) |
| Version | 0.37 (the program reports 0.37, 21 Oct 2005); Debian package `antiword_0.37-16_amd64.deb` |
| Copyright | Copyright (C) 1998-2003 A.J. (Adri) van Os, `antiword@winfield.demon.nl` |
| Source | http://www.winfield.demon.nl/index.html (as written in the Debian package's `copyright` file) |
| Licence | GNU General Public License. The Debian `copyright` file says "GNU General Public License (GPL)" without a version number; GPL-2.0 is the version to confirm |
| Files | `third_party/antiword/antiword_0.37-16_amd64.deb`, `third_party/antiword/bin/antiword`, `third_party/antiword/share/` (character tables and `fontnames` from the same package) |

antiword is run as a separate program (a subprocess), never linked into the application. The licence text and the source
of the package are available from the Debian archive (package `antiword`, version 0.37-16); to confirm the exact source
URL the team wants to cite.

## fastText language-identification model

| | |
|---|---|
| Used for | Guessing the language of a chat question (`third_party/fasttext/lid.176.ftz`) |
| Model | `lid.176.ftz`, the compressed language-identification model for 176 languages |
| Copyright | Facebook, Inc. / Meta Platforms, Inc. (fastText authors) |
| Source | https://fasttext.cc/docs/en/language-identification.html |
| Licence | Creative Commons Attribution-ShareAlike (CC-BY-SA). The repository does not contain the licence text. fasttext.cc publishes the model under CC-BY-SA 3.0; the version (3.0 or 4.0) is to confirm |
| Files | `third_party/fasttext/lid.176.ftz` |

The `fasttext-wheel` package that loads the model (`requirements.txt`, version 0.9.2) is a separate component under the
fastText MIT licence (to confirm).
