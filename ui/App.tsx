import {
  ArrowDownToLine,
  Box,
  Cable,
  Check,
  ChevronRight,
  CircleHelp,
  Cpu,
  Folder,
  FolderOpen,
  Layers3,
  LibraryBig,
  LoaderCircle,
  Package,
  Plus,
  Search,
  Settings2,
  Trash2,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, Library, Part } from "./api";
import ImportDialog from "./ImportDialog";
import ComponentPreviews from "./ComponentPreviews";
import CatalogPreviews from "./CatalogPreviews";
import {
  closeGuard,
  desktopMessage,
  isDesktop,
  nativeRequest,
} from "./desktop";
import { droppedFiles, UploadEntry } from "./files";
import { BOM, Setup } from "./SettingsPanels";

type Notice = { text: string; error?: boolean } | null;
const icon = (type: string) =>
  type === "connector" ? (
    <Cable size={18} />
  ) : type === "IC" ? (
    <Cpu size={18} />
  ) : (
    <Package size={18} />
  );
export default function App() {
  const searchInput = useRef<HTMLInputElement>(null);
  const detailDialog = useRef<HTMLElement>(null);
  const [focusSearch, setFocusSearch] = useState(0);
  const [dropEntries, setDropEntries] = useState<UploadEntry[]>([]);
  const [library, setLibrary] = useState<Library | null>(null),
    [loading, setLoading] = useState(true),
    [notice, setNotice] = useState<Notice>(null);
  const [search, setSearch] = useState(""),
    [category, setCategory] = useState(""),
    [view, setView] = useState("catalog"),
    [importing, setImporting] = useState(false);
  const [path, setPath] = useState(""),
    [working, setWorking] = useState(false),
    [selected, setSelected] = useState<Part | null>(null),
    [editing, setEditing] = useState<string>("");
  const notify = (text: string, error = false) => setNotice({ text, error });
  const refresh = useCallback(async () => {
    try {
      const s = await api("state");
      setLibrary(s.library);
      if (s.startupWarning) setNotice({ text: s.startupWarning, error: true });
    } catch (e) {
      setNotice({ text: String(e), error: true });
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    refresh();
  }, [refresh]);
  useEffect(() => {
    closeGuard("app", { dirty: false, busy: working });
    return () => closeGuard("app");
  }, [working]);
  useEffect(() => {
    const openLibrary = () => {
      if (importing || working) return;
      setPath(library?.path || "");
      setView("open");
    };
    window.addEventListener("manager-open-library", openLibrary);
    return () =>
      window.removeEventListener("manager-open-library", openLibrary);
  }, [importing, working, library]);
  useEffect(() => {
    const find = () => {
      if (!library || importing || selected || working) return;
      setView("catalog");
      setFocusSearch((request) => request + 1);
    };
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "f") {
        event.preventDefault();
        find();
      }
    };
    window.addEventListener("keydown", shortcut);
    window.addEventListener("manager-find", find);
    return () => {
      window.removeEventListener("keydown", shortcut);
      window.removeEventListener("manager-find", find);
    };
  }, [library, importing, selected, working]);
  useEffect(() => {
    if (!focusSearch) return;
    searchInput.current?.focus();
    searchInput.current?.select();
  }, [focusSearch]);
  async function open(create = false) {
    setWorking(true);
    try {
      setLibrary(await api(create ? "create" : "open", { path }));
      notify(
        create
          ? "Your library is ready. Add your first component."
          : "Library opened.",
      );
    } catch (e) {
      notify(String(e), true);
    } finally {
      setWorking(false);
    }
  }
  async function browse() {
    try {
      const r = isDesktop()
        ? { path: await nativeRequest<string>("choose-library") }
        : await api("pick-folder", {});
      if (r.path) setPath(r.path);
    } catch (e) {
      notify(String(e), true);
    }
  }
  const parts = (library?.parts || []).filter(
    (p) =>
      p.Type !== "accessory" &&
      (!category ||
        p.Category === category ||
        p.Category.startsWith(category + "/")) &&
      [
        p.MPN,
        p.Manufacturer,
        p.Description,
        p.Value,
        p.Category,
        p.SearchPath,
        p.Package,
      ].some((v) => v?.toLowerCase().includes(search.toLowerCase())),
  );
  const passiveType =
    category === "components/resistors" ||
    category.startsWith("components/resistors/")
      ? "resistor"
      : category === "components/capacitors" ||
          category.startsWith("components/capacitors/")
        ? "capacitor"
        : "";
  const selectedIndex = selected ? parts.findIndex((part) => part.ID === selected.ID) : -1;
  function navigateComponent(direction: number) {
    const next = parts[selectedIndex + direction];
    if (next && selectedIndex >= 0) setSelected(next);
  }
  useEffect(() => {
    if (selected) detailDialog.current?.focus();
  }, [!!selected]);
  useEffect(() => {
    if (!selected) return;
    const navigate = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      const target = event.target as HTMLElement;
      if (target.closest('input, textarea, select, [contenteditable="true"]')) return;
      if (document.fullscreenElement || document.querySelector('.native-fullscreen')) return;
      if (event.key === "Escape") { event.preventDefault(); setSelected(null); }
      else if (["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)) {
        event.preventDefault();
        navigateComponent(event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1);
      }
    };
    document.addEventListener("keydown", navigate);
    return () => document.removeEventListener("keydown", navigate);
  }, [selected, selectedIndex, parts]);
  return (
    <div
      className="shell"
      onDragOver={(e) => {
        if (library && e.dataTransfer.types.includes("Files"))
          e.preventDefault();
      }}
      onDrop={async (e) => {
        e.preventDefault();
        if (!library || importing || working) return;
        setWorking(true);
        try {
          const entries = await droppedFiles(e.dataTransfer.items);
          if (entries.length) {
            setDropEntries(entries);
            setEditing("");
            setSelected(null);
            setImporting(true);
          }
        } catch (error) {
          notify(String(error), true);
        } finally {
          setWorking(false);
        }
      }}
    >
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <Layers3 size={22} />
          </div>
          <span>
            Library<span className="brand-sub">MANAGER</span>
          </span>
        </div>
        <div className="library-pill">
          <LibraryBig size={17} />
          <div>
            <strong>{library?.name || "No library open"}</strong>
            <small>{library ? "LOCAL LIBRARY" : "GET STARTED"}</small>
          </div>
          <span className="status-dot" />
        </div>
        <div className="nav-label">WORKSPACE</div>
        <button
          className={
            "nav-item " + (view === "catalog" && !category ? "active" : "")
          }
          onClick={() => {
            setView("catalog");
            setCategory("");
          }}
        >
          <LayoutIcon />
          All components
          <span>
            {library?.parts.filter((p) => p.Type !== "accessory").length || 0}
          </span>
        </button>
        <button
          className={"nav-item " + (view === "setup" ? "active" : "")}
          onClick={() => setView("setup")}
        >
          <Settings2 size={17} />
          KiCad connection
        </button>
        <button
          className={"nav-item " + (view === "bom" ? "active" : "")}
          onClick={() => setView("bom")}
        >
          <ArrowDownToLine size={17} />
          BOM export
        </button>
        <div className="nav-label spaced">
          FOLDERS{" "}
          <button
            title="Create category folder"
            onClick={async () => {
              if (!library) return;
              const name = prompt(
                "Folder path within the library",
                "components/",
              );
              if (name)
                try {
                  setLibrary(
                    await api("folder", {
                      path: name,
                      revision: library.revision,
                    }),
                  );
                } catch (e) {
                  notify(String(e), true);
                }
            }}
          >
            <Plus size={15} />
          </button>
        </div>
        <div className="folder-tree">
          {(library?.categories || [])
            .filter(
              (c) =>
                !/^components\/accessories(\/|$)/.test(c) ||
                library?.parts.some(
                  (p) =>
                    p.Type !== "accessory" &&
                    (p.Category === c || p.Category.startsWith(c + "/")),
                ),
            )
            .map((c) => (
              <button
                key={c}
                className={
                  "folder-item " +
                  (category === c && view === "catalog" ? "active" : "")
                }
                style={{ paddingLeft: 16 + (c.split("/").length - 1) * 13 }}
                onClick={() => {
                  setCategory(c);
                  setView("catalog");
                }}
              >
                <Folder size={15} />
                <span>{c.split("/").pop()?.replaceAll("_", " ")}</span>
              </button>
            ))}
        </div>
        <div className="sidebar-bottom">
          <div>
            <ShieldCheck size={16} />
            <span>Local files. Portable library.</span>
          </div>
          <button
            onClick={() => {
              setPath(library?.path || "");
              setView("open");
            }}
          >
            <FolderOpen size={16} />
            Open another library
          </button>
          <small>KiCad Library Manager · 0.3</small>
          <button
            onClick={async () => {
              if (isDesktop()) {
                desktopMessage({ type: "quit" });
                return;
              }
              if (
                importing &&
                !confirm("Close the manager and discard the open editor?")
              )
                return;
              try {
                await api("quit", {});
                notify("Library Manager stopped. You can close this window.");
              } catch (e) {
                notify(String(e), true);
              }
            }}
          >
            Quit manager
          </button>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            <span>Workspace</span>
            <ChevronRight size={14} />
            <strong>{library?.name || "Welcome"}</strong>
          </div>
          <span className="local-badge">
            <span /> Runs on your computer
          </span>
        </header>
        {notice && (
          <div
            className={"notice " + (notice.error ? "error" : "")}
            role="status"
          >
            {notice.error ? <CircleHelp size={18} /> : <Check size={18} />}
            <span>{notice.text}</span>
            <button onClick={() => setNotice(null)}>
              <X size={16} />
            </button>
          </div>
        )}
        {loading ? (
          <div className="loading">
            <LoaderCircle className="spin" />
            Opening workspace…
          </div>
        ) : !library || view === "open" ? (
          <div className="welcome">
            <div className="eyebrow">A HOME FOR EVERY COMPONENT</div>
            <h1>
              Your library.
              <br />
              Everything together.
            </h1>
            <p>
              Models, part information, and connector accessories.
              <br />
              Organized in a folder you can take with you.
            </p>
            <div className="welcome-card">
              <h3>
                {library
                  ? "Open a different library"
                  : "Start with a library folder"}
              </h3>
              <p>
                Choose an existing library, or enter a new empty folder to
                create one.
              </p>
              <label>Library folder</label>
              <div className="inline">
                <input
                  placeholder="/Users/you/Documents/My Library"
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                />
                <button className="secondary" onClick={browse}>
                  <FolderOpen size={17} />
                  Browse
                </button>
              </div>
              <div className="actions">
                <button
                  className="primary"
                  disabled={working || !path}
                  onClick={() => open(true)}
                >
                  <Plus size={17} />
                  Create library
                </button>
                <button
                  className="secondary"
                  disabled={working || !path}
                  onClick={() => open()}
                >
                  <FolderOpen size={17} />
                  Open library
                </button>
                {library && (
                  <button
                    className="text-button"
                    onClick={() => setView("catalog")}
                  >
                    Back
                  </button>
                )}
              </div>
              <small>
                New libraries include reusable models, an empty catalog, and
                Mac/Windows launchers.
              </small>
            </div>
          </div>
        ) : view === "setup" ? (
          <Setup library={library} notify={notify} />
        ) : view === "bom" ? (
          <BOM library={library} notify={notify} />
        ) : (
          <div className="content">
            <div className="page-heading">
              <div>
                <div className="eyebrow">COMPONENT CATALOG</div>
                <h1>
                  {category
                    ? category.split("/").pop()?.replaceAll("_", " ")
                    : "All components"}
                  <span className="count">{parts.length}</span>
                </h1>
                <p>
                  Your parts, their models, and everything needed to use them.
                </p>
              </div>
              <button className="primary" onClick={() => setImporting(true)}>
                <Plus size={18} />
                Add component
              </button>
            </div>
            <div className="summary-strip">
              <div>
                <span className="summary-icon">
                  <Package />
                </span>
                <strong>
                  {library.parts.filter((p) => p.Type !== "accessory").length}
                  <small>Components</small>
                </strong>
              </div>
              <div>
                <span className="summary-icon">
                  <Cable />
                </span>
                <strong>
                  {library.parts.filter((p) => p.Type === "connector").length}
                  <small>Connectors</small>
                </strong>
              </div>
              <div className="path-summary">
                <FolderOpen size={18} />
                <span title={library.path}>{library.path}</span>
              </div>
            </div>
            {library.recoveryNeeded && (
              <div className="notice error">
                Another editor or an interrupted save has locked this library.
                Close other editors before recovering. Any interrupted save will
                be restored from its transaction backup.
                <button
                  className="secondary"
                  onClick={async () => {
                    if (
                      !confirm(
                        "Confirm every other library editor is closed. Restore the exact pre-save files from the transaction backup?",
                      )
                    )
                      return;
                    try {
                      setLibrary(await api("recover", { writerStopped: true }));
                      notify("Interrupted save recovered.");
                    } catch (e) {
                      notify(String(e), true);
                    }
                  }}
                >
                  Recover interrupted save
                </button>
              </div>
            )}
            {!!library.pathIssues?.length && (
              <div className="notice error path-issues">
                <strong>Some component folders need attention</strong>
                <ul>
                  {library.pathIssues.map((issue, i) => (
                    <li key={i}>
                      {typeof issue === "string"
                        ? issue
                        : issue.message ||
                          [issue.partID, issue.path].filter(Boolean).join(": ")}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {passiveType && (
              <div className="passive-sheet-tabs">
                <button
                  className={passiveType === "resistor" ? "selected" : ""}
                  onClick={() => setCategory("components/resistors")}
                >
                  Resistors
                </button>
                <button
                  className={passiveType === "capacitor" ? "selected" : ""}
                  onClick={() => setCategory("components/capacitors")}
                >
                  Capacitors
                </button>
                <span>One row per part number · shared models</span>
              </div>
            )}
            <div className="table-tools">
              <div className="search">
                <Search size={18} />
                <input
                  ref={searchInput}
                  aria-label="Search components"
                  placeholder="Search part numbers, descriptions, or folders…"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <kbd>⌘F</kbd>
              </div>
              <button className="secondary" onClick={refresh}>
                <SlidersHorizontal size={16} />
                Refresh
              </button>
            </div>
            <div
              className={"catalog" + (passiveType ? " passive-catalog" : "")}
            >
              <div className="table-head">
                <span>COMPONENT</span>
                {passiveType ? (
                  <>
                    <span>VALUE</span>
                    <span>PACKAGE</span>
                    <span>TOLERANCE</span>
                    <span>
                      {passiveType === "resistor" ? "POWER" : "VOLTAGE"}
                    </span>
                  </>
                ) : (
                  <>
                    <span>CATEGORY</span>
                    <span>PREVIEWS</span>
                  </>
                )}
                <span />
              </div>
              {parts.map((p) => (
                <button
                  key={p.ID}
                  className={`part-row${selected?.ID === p.ID ? " selected-part" : ""}`}
                  onClick={() => setSelected(p)}
                >
                  <span className="part-identity">
                    <span className="part-icon">{icon(p.Type)}</span>
                    <span>
                      <strong>{p.MPN}</strong>
                      <small>
                        {p.Manufacturer} · {p.Description || p.Value}
                      </small>
                      {passiveType && <CatalogPreviews key={`${library!.id}:${library!.revision}:${p.ID}`} partID={p.ID} revision={`${library!.id}:${library!.revision}`} />}
                    </span>
                  </span>
                  {passiveType ? (
                    <>
                      <span className="passive-value">{p.Value || "—"}</span>
                      <span>{p.Package || "—"}</span>
                      <span>{p.Tolerance || "—"}</span>
                      <span>
                        {p[
                          passiveType === "resistor" ? "Power_W" : "Voltage_VDC"
                        ] || "—"}
                      </span>
                    </>
                  ) : (
                    <>
                      <span className="category-text">
                        {p.Category.replaceAll("/", " / ")}
                      </span>
                      <CatalogPreviews key={`${library!.id}:${library!.revision}:${p.ID}`} partID={p.ID} revision={`${library!.id}:${library!.revision}`} />
                    </>
                  )}
                  <ChevronRight size={17} />
                </button>
              ))}
              {!parts.length && (
                <div className="empty">
                  <div className="empty-icon">
                    <Box size={36} />
                  </div>
                  <h2>
                    {search
                      ? "No matching components"
                      : "Make room for your first part"}
                  </h2>
                  <p>
                    {search
                      ? "Try a different part number, manufacturer, or category."
                      : "Paste a supplier page, drop in your CAD files, and keep everything together."}
                  </p>
                  {!search && (
                    <button
                      className="secondary"
                      onClick={() => setImporting(true)}
                    >
                      <Plus size={17} />
                      Add your first component
                    </button>
                  )}
                  <div className="empty-tags">
                    <span>Folder & ZIP import</span>
                    <span>Shared passive models</span>
                    <span>Connector accessories</span>
                  </div>
                </div>
              )}
            </div>
            <div className="catalog-footer">
              <span>
                {parts.length} component{parts.length !== 1 ? "s" : ""}
              </span>
              <span>Catalog changes are backed up automatically.</span>
            </div>
          </div>
        )}
      </main>
      {importing && library && (
        <ImportDialog
          library={library}
          editingID={editing}
          initialEntries={dropEntries}
          onClose={() => {
            setImporting(false);
            setEditing("");
            setDropEntries([]);
          }}
          onSave={(l) => {
            setLibrary(l);
            notify("Component saved with its models and accessory list.");
          }}
        />
      )}
      {selected && (
        <div className="modal-backdrop" onClick={() => setSelected(null)}>
          <section
            className="detail-dialog"
            ref={detailDialog}
            role="dialog"
            aria-modal="true"
            aria-label="Component preview"
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
          >
            <button className="close" onClick={() => setSelected(null)}>
              <X />
            </button>
            <div className="eyebrow">COMPONENT RECORD</div>
            <h2>{selected.MPN}</h2>
            <p>{selected.Manufacturer}</p>
            <div className="component-navigation">
              <button className="secondary" disabled={selectedIndex <= 0} onClick={() => navigateComponent(-1)}>← Previous</button>
              <span>{selectedIndex + 1} / {parts.length} · Use arrow keys</span>
              <button className="secondary" disabled={selectedIndex < 0 || selectedIndex >= parts.length - 1} onClick={() => navigateComponent(1)}>Next →</button>
            </div>
            <ComponentPreviews key={selected.ID} partID={selected.ID} />
            <dl>
              {[
                "Description",
                "Value",
                "Category",
                "Package",
                "Tolerance",
                "Power_W",
                "Voltage_VDC",
                "Current_A",
                "Voltage_V",
                "AssetFolder",
                "ID",
              ]
                .filter((k) => selected[k])
                .map((k) => (
                  <div key={k}>
                    <dt>{k.replaceAll("_", " ")}</dt>
                    <dd>{selected[k]}</dd>
                  </div>
                ))}
            </dl>
            {selected.Datasheet && (
              <a
                className="secondary"
                href={selected.Datasheet}
                target="_blank"
                rel="noreferrer"
              >
                Open datasheet ↗
              </a>
            )}
            <button
              className="primary"
              onClick={() => {
                setEditing(selected.ID);
                setSelected(null);
                setImporting(true);
              }}
            >
              <Settings2 size={16} />
              Edit component
            </button>
            <button
              className="danger"
              onClick={async () => {
                try {
                  const plan = await api(
                    "delete-plan?part=" + encodeURIComponent(selected.ID),
                  );
                  const extra = plan.accessories.length
                    ? `

Its accessories are removed too: ${plan.accessories.join(", ")}`
                    : "";
                  if (
                    !window.confirm(
                      `Delete ${plan.part} from the library?

` +
                        `This removes its symbol, footprint and 3D model (${plan.files.length} files).${extra}

` +
                        "The previous files are kept in the library backups.",
                    )
                  )
                    return;
                  const r = await api("delete", { partID: selected.ID });
                  setLibrary(r.library);
                  setSelected(null);
                  notify(`Deleted ${plan.part}.`);
                } catch (e) {
                  notify(String(e), true);
                }
              }}
            >
              <Trash2 size={16} />
              Delete component
            </button>
            <h3>Accessory list</h3>
            {selected.recipe?.items.length ? (
              selected.recipe.items.map((i: any) => (
                <p key={i.partID}>
                  {i.quantity} ×{" "}
                  {library?.parts.find((p) => p.ID === i.partID)?.MPN ||
                    i.partID}{" "}
                  <small>
                    {
                      library?.parts.find((p) => p.ID === i.partID)
                        ?.Manufacturer
                    }{" "}
                    · {i.role}
                  </small>
                </p>
              ))
            ) : (
              <p className="muted">No accessories attached.</p>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function LayoutIcon() {
  return <LibraryBig size={17} />;
}
