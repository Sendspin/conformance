# aiosendspin patches

Patches applied, in file-name order, to the aiosendspin bundled under
`src/conformance/_vendor/`. `scripts/vendor_aiosendspin.py` applies them with
`git apply` from the upstream repository root, before it rewrites imports, so
each one is an ordinary upstream diff (`git format-patch` output works as is).
Name them `NNNN-<topic>.patch`.

Each patch is also opened as an upstream aiosendspin PR. Once an upstream
release carries all of them, the bundled copy, this directory and the script
are removed in favour of that release.
