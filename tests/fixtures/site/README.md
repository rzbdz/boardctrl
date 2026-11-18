# A site, in every file it can have

This directory is a complete site: the private facts a board host reads besides the code and the shipped
platform knowledge. Copy it to your own location (`cp -r tests/fixtures/site ~/my-site`) and edit what is
yours; nothing here reaches a real board.

    hosts/       one YAML per board host: its id, port, modules, hooks' env, the registry it announces to
    duts/        the boards the site declares: which provider fills each role of each DUT
    boards/      header pin maps (a 40-pin and a 26-pin header, both made up)
    socs/        SoC facts: boot-ROM fingerprints, download mode, USB ids
    sdk/         families, servers, workspaces, instances: the SDK catalog
    hooks/       executables a host runs for probe and flash steps
    hints/       notes a person or an agent reads
    providers/   a site provider: `providers/<name>/provider.py` with `setup(ctx, api, config)`

A site has the same shape as each directory under `platforms/` in this repository, and later layers override
earlier ones file by file. The test suite runs against this site, so every format here is one that works.

    BOARDCTRL_SITE=$PWD/tests/fixtures/site boardctrld -c tests/fixtures/site/hosts/dev-mock.yaml
