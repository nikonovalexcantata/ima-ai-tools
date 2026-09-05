# ima-ai-tools

A marketplace of Claude Code skills for the team.

This repository is a catalog. Each entry points to a skill in one of two places. Most entries point to a skill in a public repository, and the owner of that repository keeps the code. Some entries point to a plugin in the `plugins/` folder of this repository. We write these plugins ourselves.

## Use

### Add the marketplace

Do this one time on each computer.

```
claude plugin marketplace add nikonovalexcantata/ima-ai-tools
```

### Install a skill

Use the name of the skill from the catalog.

```
claude plugin install <skill-name>@ima-ai-tools
```

### Update the catalog

New skills come into the catalog with time. This command gets them.

```
claude plugin marketplace update ima-ai-tools
```

## Catalog

The catalog page shows all of the skills: https://nikonovalexcantata.github.io/ima-ai-tools/

The source of the page is `docs/index.html`.

## Ask for a new skill

Make an issue. Give the address of the source repository and the problem that the skill solves.

## Add a skill that we wrote

Put the plugin in a new folder under `plugins/`. Use one folder for each plugin. The instructions for the layout are in `CLAUDE.md`.

## License

The MIT license applies to the files in this repository, including the plugins in `plugins/`. The license of each external skill stays with its owner.
