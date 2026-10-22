"""Delete a component from the library.

Runs as one backed-up library transaction: the catalog rows, the component's
folder (symbol, footprint, 3D model, recipe) and any housings/contacts that only
this component used are removed, and KiCad's generated indexes are rebuilt.
"""
from .storage import APPDATA, csv_bytes, inside


def plan(lib, part_id):
    """What deleting part_id would remove, or a ValueError explaining why it can't be."""
    rows = lib.rows()
    by_id = {r["ID"]: r for r in rows}
    if part_id not in by_id:
        raise ValueError("That component no longer exists; click Refresh")
    target = by_id[part_id]
    recipes = {r["ID"]: lib.recipe(r)["items"] for r in rows}

    users = [by_id[i] for i, items in recipes.items()
             if i != part_id and any(it["partID"] == part_id for it in items)]
    if users:
        names = ", ".join(f"{u['Manufacturer']} {u['MPN']}".strip() for u in users)
        raise ValueError(f"Can't delete: it is an accessory of {names}. Remove it from their accessory list first.")

    # Accessories (housings, contacts) used only by this component go with it.
    remove = {part_id}
    for item in recipes[part_id]:
        acc = item["partID"]
        if by_id.get(acc, {}).get("Type") == "accessory" and not any(
            acc in {it["partID"] for it in items}
            for owner, items in recipes.items() if owner not in remove | {part_id}
        ):
            remove.add(acc)

    files = []
    for ident in remove:
        row = by_id[ident]
        if row["AssetFolder"]:
            folder = inside(lib.root, row["AssetFolder"])
            files += [p.relative_to(lib.root).as_posix() for p in folder.rglob("*") if p.is_file()]
        else:
            recipe = inside(lib.root, lib.recipe_path(row))
            if recipe.is_file():
                files.append(recipe.relative_to(lib.root).as_posix())
    label = lambda r: f"{r['Manufacturer']} {r['MPN']}".strip()
    return dict(
        part=label(target),
        accessories=sorted(label(by_id[i]) for i in remove - {part_id}),
        ids=sorted(remove),
        files=sorted(files),
        folders=sorted({by_id[i]["AssetFolder"] for i in remove} - {""}),
    )


def delete(lib, part_id):
    p = plan(lib, part_id)
    keep = [r for r in lib.rows() if r["ID"] not in set(p["ids"])]
    writes = {APPDATA + "/components.csv": csv_bytes(keep), **lib.compiled(keep)}
    lib.transaction(writes, lib.revision(), deletes=p["files"])
    # the transaction removes files; tidy the now-empty component folder(s)
    for ident_folder in p["folders"]:
        folder = inside(lib.root, ident_folder)
        for d in sorted((x for x in folder.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)):
            if not any(d.iterdir()):
                d.rmdir()
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return p
