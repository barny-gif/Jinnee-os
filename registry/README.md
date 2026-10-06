# jinnee-os.com – the "mother site" and the registry

## What it is
A static registry: JSON files plus downloadable packages. An installed Jinnee OS **does not phone home** for anything else – it only reads this. The owner's machine/VPS stays independent but can update.

```
jinnee-os.com/registry/
  index.json              – packs and connectors with versions
  packs/ecom.json         – pack manifest + download URL + changelog
  connectors/billingo.json
  ...
```

## How an install updates
1. The lead reads `index.json` once a day.
2. If an installed pack/connector has a newer version: one line in the morning brief – "Billingo connector 0.2 available: X fixed. Update?"
3. Owner says yes → Forge downloads it, **keeps the old one as `.bak`**, tests it on the owner's account with one read operation, reports. On error: automatic rollback.
4. Never updates without asking – updating (`update_from_registry`) is level 1 and locked there.

## Swapping a connector (e.g. Billingo → Számlázz.hu)
The owner says it on the website or to the lead: "switching to Számlázz.hu".
1. Forge downloads the `szamlazz` connector from the registry.
2. Asks for credentials following its `auth.md`, step by step, in chat.
3. Runs an `invoicing.list` test.
4. Changes `pack.json` → `connectors.defaults.invoicing`.
5. The old connector stays, inactive – switching back is one sentence.
The other agents **notice nothing**, because they call the same operation set.

## The website (v1, minimal)
- Home: what Jinnee OS is, who it's for, 3 pack cards (ecom, booking, services), "Request setup" button (the managed package).
- /registry: packs and connectors, version, changelog, capabilities.
- /docs: installation, onboarding, autonomy levels explained.
- Login, licence keys, payments: **v2**. In v1 the registry is public; revenue is setup + monthly plan.

## Versioning
Semver. Pack minor = new agent, new required connector, or a change in the shape of `pack.json`; patch = persona fix. Connector minor = new operation; major = auth change (always goes to the owner).
