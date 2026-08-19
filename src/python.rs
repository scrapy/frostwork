//! PyO3 bindings — expose the one-pass `extract` primitive to Python as the native module
//! `frostwork._frostwork`. Deliberately minimal: only the hot path crosses the FFI boundary. The
//! ergonomic `Page`/`Item` layer and the web-poet integration ride on top of this in pure Python
//! (`python/frostwork/`), so there is nothing to keep in sync between two implementations of the
//! matching logic — there is only one (the Rust core).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A schema over the fixed-width bitset budgets is a *caller* bug (too many selectors), distinct from
/// an unsupported *query* (which is contract-defined to yield an empty column). Silence would be the
/// worst outcome — the caller would just see empty columns — so raise `ValueError` instead.
fn check_budget(queries: &[String], groups: &[crate::GroupQuery]) -> PyResult<()> {
    budget_error(crate::budget_usage(queries, groups))
}

/// The `(container, [(name, selector)])` tuples Python passes, as the engine's `GroupQuery` list.
fn group_queries(groups: Vec<(String, Vec<(String, String)>)>) -> Vec<crate::GroupQuery> {
    groups
        .into_iter()
        .map(|(container, subfields)| crate::GroupQuery { container, subfields })
        .collect()
}

/// Raise `ValueError` if a schema's `(members, sibling-bits)` demand exceeds the fixed-width budget.
fn budget_error((members, sib): (usize, usize)) -> PyResult<()> {
    if members > crate::MAX_MEMBERS {
        return Err(PyValueError::new_err(format!(
            "frostwork: schema has {members} member selectors, over the limit of {} \
             (each comma-group member and each group container counts as one). Split the schema.",
            crate::MAX_MEMBERS
        )));
    }
    if sib > crate::MAX_SIB_BITS {
        return Err(PyValueError::new_err(format!(
            "frostwork: schema needs {sib} sibling-combinator trigger bits, over the limit of {}. \
             Reduce the number of `+`/`~` selectors.",
            crate::MAX_SIB_BITS
        )));
    }
    Ok(())
}

/// The canonical WHATWG encoding name for `label` (e.g. `"UTF-8"`), or `None` if this crate does not
/// recognize it. Shared by the `resolve_label` binding and [`Html::encoding`], so the two cannot drift.
fn canonical_label(label: &str) -> Option<&'static str> {
    encoding_rs::Encoding::for_label(label.as_bytes()).map(|e| e.name())
}

/// The WHATWG encoding a label names *via Python's codec set* — `codecs.lookup(label).name` fed back
/// through [`canonical_label`]. `None` when Python does not know the label, or knows it but WHATWG does
/// not name the result.
///
/// This is deliberately the same two-step `frostwork.page._check_encoding` performs, because the two
/// must agree about every label. `latin-1` is the case that forces it: WHATWG defines `iso-8859-1` and
/// not `latin-1`, so WHATWG alone cannot see that `latin-1` means windows-1252 and would let a
/// mojibake-producing label through. `utf-8-sig` is the case that forces the second `None`: Python
/// resolves it and WHATWG does not name it, so the label is ignored and the text scanned as UTF-8 —
/// which is what it is.
fn whatwg_via_python_codecs(py: Python<'_>, label: &str) -> Option<&'static str> {
    let codecs = py.import("codecs").ok()?;
    let info = codecs.call_method1("lookup", (label,)).ok()?;
    let name: String = info.getattr("name").ok()?.extract().ok()?;
    canonical_label(&name)
}

/// The document to scan, as Python hands it over: raw `bytes` off the wire, or an already-decoded
/// `str` (a browser snapshot — `web_poet.BrowserResponse.html`, `AnyResponse.text`).
///
/// `str` is here so callers do not have to write `.encode("utf-8")`, which allocates a whole second
/// copy of the document per response. On CPython >= 3.10 `&str` extraction is
/// `PyUnicode_AsUTF8AndSize`: for a compact-ASCII `str` that is a pointer INTO the string object, so
/// the copy disappears entirely; for a non-ASCII `str` CPython transcodes once and caches the result
/// ON the string, so a second page object over the same response is free. Either way Frostwork, not
/// the caller, owns the conversion.
///
/// It is NOT free in general: a non-ASCII Python `str` is stored as UCS-2/UCS-4, so its first
/// UTF-8 view has to be built. That cost is imposed by the representation, not by this crate — a
/// caller holding the original bytes should pass those.
enum Html<'a> {
    // `bytes` first: the documented, preferred input, and the only one on the crawl hot path.
    Bytes(&'a [u8]),
    Str(&'a str),
}

// Hand-written rather than `#[derive(FromPyObject)]`: the derive cannot tie a borrowed variant's
// lifetime to the input object's, so it rejects an enum holding `&'a [u8]` / `&'a str`. Both arms
// delegate to PyO3's own borrowing extractors, so neither copies.
impl<'a, 'py> FromPyObject<'a, 'py> for Html<'a> {
    type Error = PyErr;

    fn extract(ob: pyo3::Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        if let Ok(bytes) = <&'a [u8] as FromPyObject>::extract(ob) {
            return Ok(Html::Bytes(bytes));
        }
        if let Ok(text) = <&'a str as FromPyObject>::extract(ob) {
            return Ok(Html::Str(text));
        }
        Err(pyo3::exceptions::PyTypeError::new_err(format!(
            "frostwork: html must be bytes (preferred - the engine tokenizes raw bytes) or str \
             (already-decoded text, e.g. a browser snapshot), got {}. For a bytearray/memoryview use \
             the frostwork.extract wrapper, which converts them.",
            ob.get_type().name().map(|n| n.to_string()).unwrap_or_else(|_| "?".into())
        )))
    }
}

impl Html<'_> {
    fn as_bytes(&self) -> &[u8] {
        match self {
            Html::Bytes(b) => b,
            Html::Str(s) => s.as_bytes(),
        }
    }

    /// The encoding to scan with, given the caller's label.
    ///
    /// For `bytes` the label passes through: the engine sniffs when it is `None`, exactly as before.
    ///
    /// For `str` the bytes handed on are UTF-8 by construction, so there is nothing to sniff and
    /// nothing to guess — the answer is UTF-8, and a `<meta charset>` surviving in a browser snapshot
    /// is correctly ignored.
    ///
    /// Only a label that resolves to a DIFFERENT encoding is refused, because that one would decode
    /// UTF-8 bytes as, say, cp1252 and quietly produce mojibake — the plausible-wrong-value the
    /// no-fallback contract exists to rule out. A label nothing can resolve is *not* refused: the
    /// engine's rule for one is WHATWG's "failure, continue" (ignore it and sniff), and for
    /// already-decoded text ignoring it lands on UTF-8, which is right.
    ///
    /// Resolution has to consult BOTH label universes, or the same argument means different things
    /// through the two entry points. WHATWG defines `iso-8859-1` but not `latin-1`, and Python defines
    /// `utf_8`/`utf-8-sig`/`U8` which WHATWG does not — so `frostwork.extract` (which normalizes through
    /// `codecs`) and a direct `Plan` call (which does not go through it, and is the path
    /// `frostwork.webpoet` uses) would disagree on both sets. `codecs` is consulted only when this
    /// crate cannot resolve the label itself, so the common path stays inside Rust.
    fn encoding<'e>(&self, py: Python<'_>, label: Option<&'e str>) -> PyResult<Option<&'e str>> {
        let Html::Str(_) = self else {
            return Ok(label); // bytes: pass the label through, `None` still sniffs
        };
        let Some(l) = label else { return Ok(Some("utf-8")) };
        let resolved = canonical_label(l).or_else(|| whatwg_via_python_codecs(py, l));
        match resolved {
            Some(name) if name != "UTF-8" => Err(PyValueError::new_err(format!(
                "frostwork: html is already-decoded str (tokenized as UTF-8), but encoding={l:?} \
                 resolves to {name}, which would decode those bytes wrongly — pass the original bytes \
                 with the label, or drop the label."
            ))),
            _ => Ok(Some("utf-8")),
        }
    }
}

/// A schema compiled ONCE and reused across pages — the native object behind `frostwork.Page` /
/// `FrostPage`, which build a `Plan` a single time and call it per response instead of re-sending
/// string selectors (and re-parsing them) every page. The budget is validated at construction, so an
/// over-budget schema raises `ValueError` here — once — not on every extract.
#[pyclass]
struct Plan {
    inner: crate::Plan,
}

#[pymethods]
impl Plan {
    /// Compile `flat_queries` + `groups` (the shapes `extract_grouped` accepts). Raises `ValueError`
    /// if the schema exceeds the member / sibling-bit budget.
    ///
    /// `first_only[c]` declares that flat column `c`'s consumer keeps only the FIRST value — the
    /// single-valued cardinality `Page.field` / web-poet `field(all=False)` apply. When every column
    /// says so and the schema is of an eligible shape, the scan STOPS once each has a value instead of
    /// running to EOF. The columns are unchanged apart from values the caller was going to discard, so
    /// the item is identical; see `crate::Plan::compile_first_only` for what disarms it. Omitted (the
    /// default) means "keep everything", which is what a caller that has not thought about cardinality
    /// must get.
    #[new]
    #[pyo3(signature = (flat_queries, groups, first_only=None))]
    fn new(
        flat_queries: Vec<String>,
        groups: Vec<(String, Vec<(String, String)>)>,
        first_only: Option<Vec<bool>>,
    ) -> PyResult<Self> {
        let first_only = first_only.unwrap_or_default();
        let inner =
            crate::Plan::compile_first_only(&flat_queries, &group_queries(groups), &first_only);
        budget_error(inner.budget_usage())?;
        Ok(Plan { inner })
    }

    /// One streaming pass over `html`, returning one value-column per flat query (query order).
    /// The GIL is released for the duration of the scan (`html` is an immutable buffer and
    /// the compiled plan is read-only), so concurrent extracts on a thread pool run in parallel.
    #[pyo3(signature = (html, encoding=None))]
    fn extract(
        &self,
        py: Python<'_>,
        html: Html<'_>,
        encoding: Option<&str>,
    ) -> PyResult<Vec<Vec<String>>> {
        let encoding = html.encoding(py, encoding)?;
        let bytes = html.as_bytes();
        Ok(py.detach(|| self.inner.extract(bytes, encoding).0))
    }

    /// One streaming pass returning `(flat_columns, grouped)` — see the `extract_grouped` free function.
    /// Releases the GIL for the duration of the scan, like `extract`.
    #[pyo3(signature = (html, encoding=None))]
    #[allow(clippy::type_complexity)]
    fn extract_grouped(
        &self,
        py: Python<'_>,
        html: Html<'_>,
        encoding: Option<&str>,
    ) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>)> {
        let encoding = html.encoding(py, encoding)?;
        let bytes = html.as_bytes();
        Ok(py.detach(|| self.inner.extract(bytes, encoding)))
    }
}

/// One streaming pass over `html`, returning one value-column per query (in query order) — the exact
/// output of [`crate::extract`]. `html` is `bytes` (or a `bytes` subclass such as web-poet's
/// `HttpResponseBody`) or an already-decoded `str` — see [`Html`]; the pure-Python `frostwork.extract`
/// wrapper converts the remaining bytes-likes.
/// `encoding` is an optional charset label as Scrapy passes from `Content-Type`; `None` sniffs
/// (BOM → `<meta>` → UTF-8). Unsupported queries yield an empty column — there is no fallback.
/// Raises `ValueError` if the schema exceeds the member/sibling-bit budget (a caller bug).
#[pyfunction]
#[pyo3(signature = (html, queries, encoding=None))]
fn extract(
    py: Python<'_>,
    html: Html<'_>,
    queries: Vec<String>,
    encoding: Option<&str>,
) -> PyResult<Vec<Vec<String>>> {
    check_budget(&queries, &[])?;
    let encoding = html.encoding(py, encoding)?;
    let bytes = html.as_bytes();
    Ok(py.detach(|| crate::extract(bytes, &queries, encoding)))
}

/// One streaming pass returning `(flat_columns, grouped)`. `groups` is a list of
/// `(container_selector, [(subfield_name, subfield_selector), ...])`; `grouped[g]` is that group's
/// rows in document order, each row a list of sub-field value-columns (`[group][row][subfield][value]`).
/// Sub-field names are carried by the caller (the pure-Python `Page`/`webpoet` layer); the engine keys
/// sub-columns positionally. Same one-pass, no-DOM, no-fallback semantics as `extract`.
#[pyfunction]
#[pyo3(signature = (html, flat_queries, groups, encoding=None))]
#[allow(clippy::type_complexity)]
fn extract_grouped(
    py: Python<'_>,
    html: Html<'_>,
    flat_queries: Vec<String>,
    groups: Vec<(String, Vec<(String, String)>)>,
    encoding: Option<&str>,
) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>)> {
    let gq = group_queries(groups);
    check_budget(&flat_queries, &gq)?;
    let encoding = html.encoding(py, encoding)?;
    let bytes = html.as_bytes();
    Ok(py.detach(|| crate::extract_grouped(bytes, &flat_queries, &gq, encoding)))
}

/// Resolve `label` against the engine's charset-label set (WHATWG labels), returning the canonical
/// encoding name (e.g. `"UTF-8"`, `"windows-1252"`) or `None` if unrecognized. The pure-Python
/// layer uses this to fail fast on labels the engine would otherwise silently ignore (it would
/// fall through to BOM/`<meta>` sniffing — a plausible-wrong-decode, which the no-fallback
/// philosophy forbids surfacing silently).
#[pyfunction]
fn resolve_label(label: &str) -> Option<&'static str> {
    canonical_label(label)
}

/// The encoding `extract` would scan this document with, as a WHATWG canonical name. `encoding` is the
/// caller/HTTP charset label, or `None` to sniff. See [`crate::detect_encoding`].
///
/// An already-decoded `str` is answered `"UTF-8"` without sniffing, for the reason [`Html::encoding`]
/// gives: those bytes ARE UTF-8, and a `<meta charset>` surviving in a browser snapshot describes the
/// document the browser already decoded, not the string in hand. Reporting the meta would be reporting
/// an encoding the caller must not apply.
#[pyfunction]
#[pyo3(signature = (html, encoding=None))]
fn detect_encoding(py: Python<'_>, html: Html<'_>, encoding: Option<&str>) -> PyResult<&'static str> {
    if let Html::Str(_) = html {
        html.encoding(py, encoding)?; // refuses a label that would decode those bytes wrongly
        return Ok("UTF-8");
    }
    Ok(crate::detect_encoding(html.as_bytes(), encoding))
}

/// `Support` as a Python-facing `(supported: bool, reason: Optional[str])` tuple.
fn support_tuple(s: &crate::Support) -> (bool, Option<String>) {
    (s.is_supported(), s.reason().map(str::to_string))
}

/// The value terminal each query produces: `"text"`, `"attr"`, `"outer"`, `"normalize-space"`, or
/// `None` for a query that does not compile. `"outer"` means the column holds the matched element's raw
/// source — a NODE reference rather than a scalar — which is the one case `frostwork.webpoet` has to
/// re-parse before handing it to a field processor. Derived from the same compiler front-end `extract`
/// uses, so it cannot drift from how the query is actually routed.
#[pyfunction]
fn selector_terminals(queries: Vec<String>) -> Vec<Option<&'static str>> {
    crate::selector_terminals(&queries)
}

/// Per query, `(pinned_tag, can_match_a_synthesized_frame)` — the matched-node identity the web-poet layer
/// needs before it re-parses an outer-HTML value; see [`crate::selector_node_identity`].
#[pyfunction]
fn selector_node_identity(queries: Vec<String>) -> Vec<(Option<String>, bool)> {
    crate::selector_node_identity(&queries)
}

/// Audit a schema WITHOUT parsing any HTML: for each flat query, group container, and sub-field,
/// report whether the engine supports it (and, if not, a best-effort reason), plus the member /
/// sibling-bit budget usage against the limits. The no-fallback contract makes an unsupported selector
/// indistinguishable from a legitimately-empty field at runtime; this lets a caller check a schema up
/// front. Returns `(flat, groups, budget)` where `flat` is `[(supported, reason)]`, `groups` is
/// `[((c_supported, c_reason), [(supported, reason), ...])]`, and `budget` is
/// `(members, max_members, sib_bits, max_sib_bits)`. The pure-Python `frostwork.check` wraps this into
/// a named report; see `frostwork.page`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn audit_schema(
    flat_queries: Vec<String>,
    groups: Vec<(String, Vec<(String, String)>)>,
) -> (
    Vec<(bool, Option<String>)>,
    Vec<((bool, Option<String>), Vec<(bool, Option<String>)>)>,
    (usize, usize, usize, usize),
) {
    let a = crate::audit_schema(&flat_queries, &group_queries(groups));
    let flat = a.flat.iter().map(support_tuple).collect();
    let grouped = a
        .groups
        .iter()
        .map(|g| (support_tuple(&g.container), g.subfields.iter().map(support_tuple).collect()))
        .collect();
    (flat, grouped, (a.members, a.max_members, a.sib_bits, a.max_sib_bits))
}

#[pymodule]
fn _frostwork(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__doc__", "Native Frostwork core: treeless, one-pass HTML extraction.")?;
    m.add_function(wrap_pyfunction!(extract, m)?)?;
    m.add_function(wrap_pyfunction!(extract_grouped, m)?)?;
    m.add_function(wrap_pyfunction!(audit_schema, m)?)?;
    m.add_function(wrap_pyfunction!(selector_terminals, m)?)?;
    m.add_function(wrap_pyfunction!(selector_node_identity, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_label, m)?)?;
    m.add_function(wrap_pyfunction!(detect_encoding, m)?)?;
    m.add_class::<Plan>()?;
    Ok(())
}
