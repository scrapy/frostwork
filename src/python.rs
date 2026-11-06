//! PyO3 bindings — expose the `extract` primitive to Python as the native module
//! `frostwork._frostwork`. Deliberately minimal: only the hot path crosses the FFI boundary. The
//! ergonomic `Page`/`Item` layer and the web-poet integration ride on top of this in pure Python
//! (`python/frostwork/`), so there is nothing to keep in sync between two implementations of the
//! matching logic — there is only one (the Rust core).

use pyo3::exceptions::{PyLookupError, PyTypeError, PyValueError};
use pyo3::prelude::*;

use crate::{CompileOptions, EncodingHints, EncodingSource, GroupQuery, ParseMode, Query, Syntax};

/// The `parser=` keyword: `"browser"` (the default) or `"lxml"`, as [`ParseMode`] names them. Anything
/// else is the caller's mistake and raises `ValueError` rather than quietly picking a mode.
fn parse_mode(parser: &str) -> PyResult<ParseMode> {
    ParseMode::from_name(parser).ok_or_else(|| {
        PyValueError::new_err(format!("frostwork: parser must be 'browser' or 'lxml', got {parser:?}"))
    })
}

/// A schema over the fixed-width bitset budgets is a *caller* bug (too many selectors), distinct from
/// an unsupported *query* (which is contract-defined to yield an empty column). Silence would be the
/// worst outcome — the caller would just see empty columns — so raise `ValueError` instead.
fn check_budget(queries: &[Query], groups: &[GroupQuery<Query>]) -> PyResult<()> {
    budget_error(crate::budget_usage(queries, groups))
}

/// The `(container, [(name, selector)])` tuples Python passes, as the engine's `GroupQuery` list.
type PyGroups = Vec<(Query, Vec<(String, Query)>)>;

fn group_queries(groups: PyGroups) -> Vec<GroupQuery<Query>> {
    groups
        .into_iter()
        .map(|(container, subfields)| GroupQuery { container, subfields })
        .collect()
}

/// A query as Python spells it: a selector `str`, routed by the auto-detection rule, or a
/// `(selector, syntax)` tuple whose `syntax` is `"css"`, `"xpath"` or `None`.
impl FromPyObject<'_, '_> for Query {
    type Error = PyErr;

    fn extract(ob: pyo3::Borrowed<'_, '_, PyAny>) -> PyResult<Self> {
        if let Ok(text) = ob.extract::<String>() {
            return Ok(Query::new(text, Syntax::Auto));
        }
        let Ok((text, syntax)) = ob.extract::<(String, Option<String>)>() else {
            return Err(PyTypeError::new_err(format!(
                "frostwork: a query must be a selector str or a (selector, syntax) tuple, got {}",
                ob.get_type().name().map(|n| n.to_string()).unwrap_or_else(|_| "?".into())
            )));
        };
        let syntax = match syntax.as_deref() {
            None => Syntax::Auto,
            Some("css") => Syntax::Css,
            Some("xpath") => Syntax::XPath,
            Some(other) => {
                return Err(PyValueError::new_err(format!(
                    "frostwork: syntax must be 'css', 'xpath' or None, got {other:?}"
                )))
            }
        };
        Ok(Query::new(text, syntax))
    }
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
/// recognize it. Every label the binding resolves goes through [`caller_label`], which starts here.
fn canonical_label(label: &str) -> Option<&'static str> {
    encoding_rs::Encoding::for_label(label.as_bytes()).map(|e| e.name())
}

/// Resolve a caller label against WHATWG's labels first, then Python's codec set:
/// `codecs.lookup(label).name` fed back through [`canonical_label`].
///
/// Both label universes have to be consulted, or the same argument means different things to
/// different callers. `latin-1` is the case that forces the second: WHATWG defines `iso-8859-1` and
/// not `latin-1`, so WHATWG alone cannot see that `latin-1` means windows-1252. `utf-8-sig` is the
/// case that gives `None`: Python resolves it and WHATWG does not name it, so the label is ignored
/// and the text scanned as UTF-8, which is what it is. A label neither knows is ignored too, which is
/// WHATWG's "failure, continue".
///
/// Some canonical Python names are WHATWG encodings under a spelling WHATWG does not list:
/// `codecs.lookup("shift_jis").name` is `cp932`, and that is what `w3lib.encoding.resolve_encoding`
/// (Scrapy's `response.encoding`) hands over. Those are translated here, in the Python binding, so the
/// engine itself stays WHATWG-only.
///
/// Python has no `x-user-defined` or `replacement` codec until `frostwork.encoding` registers them, so
/// a label Python does not know is matched against their spellings with the normalization their
/// lookup applies, for the answer not to depend on whether that module has been imported.
fn caller_label(py: Python<'_>, label: &str) -> Option<&'static str> {
    canonical_label(label).or_else(|| whatwg_via_python_codecs(py, label))
}

/// [`caller_label`]'s lookup through Python's codecs, for a label WHATWG does not list.
fn whatwg_via_python_codecs(py: Python<'_>, label: &str) -> Option<&'static str> {
    let codecs = py.import("codecs").ok()?;
    let Ok(info) = codecs.call_method1("lookup", (label,)) else {
        let encodings = py.import("encodings").ok()?;
        let normalized = encodings.call_method1("normalize_encoding", (label.to_lowercase(),)).ok()?;
        return match normalized.extract::<String>().ok()?.as_str() {
            "x_user_defined" => canonical_label("x-user-defined"),
            "replacement" => canonical_label("replacement"),
            _ => None,
        };
    };
    let name: String = info.getattr("name").ok()?.extract().ok()?;
    let whatwg = match name.as_str() {
        "cp932" => "shift_jis",
        "euc_jp" => "euc-jp",
        "iso2022_jp" => "iso-2022-jp",
        "big5hkscs" | "cp950" => "big5",
        "cp949" | "euc_kr" => "euc-kr",
        "cp874" => "windows-874",
        "iso8859-16" => "iso-8859-16",
        "mac-roman" => "macintosh",
        "mac-cyrillic" => "x-mac-cyrillic",
        "utf-16-le" => "utf-16le",
        "utf-16-be" => "utf-16be",
        "hz" => "hz-gb-2312",
        "iso2022_kr" => "iso-2022-kr",
        other => other,
    };
    canonical_label(whatwg)
}

/// What [`Html::hints`] does with a label no codec knows.
#[derive(Clone, Copy)]
enum Unknown {
    Refuse,
    Ignore,
}

/// Whether Python's codecs know `label` at all, WHATWG encoding or not.
fn python_knows(py: Python<'_>, label: &str) -> bool {
    py.import("codecs").and_then(|codecs| codecs.call_method1("lookup", (label,))).is_ok()
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

    /// The encoding inputs to scan with: rank 3 decided here, for every Python entry point alike.
    ///
    /// A real encoding WHATWG lacks (`utf-7`) is ignored and resolution continues, as a browser does. A
    /// label no codec knows raises `ValueError` from the extraction entry points (`Unknown::Refuse`),
    /// because it is the caller's own mistake and silently sniffing instead would hide it. The detection
    /// entry points, `detect_encoding` and `navigation`, ignore it too (`Unknown::Ignore`): they read
    /// labels a server sent, which browsers treat as WHATWG's "failure, continue".
    ///
    /// For `bytes`, the WHATWG name, `content_type` and `url` pass through to the engine's precedence
    /// table. For `str` no rank applies: the bytes handed on are UTF-8 by construction, so the answer is
    /// UTF-8, `content_type` is ignored because it describes bytes that no longer exist, and a
    /// `<meta charset>` surviving in a browser snapshot is ignored too. A label that resolves to a
    /// DIFFERENT encoding is refused, because it would decode UTF-8 bytes as, say, cp1252 and quietly
    /// produce mojibake: the plausible-wrong-value the no-fallback contract exists to rule out.
    fn hints<'a>(
        &self,
        py: Python<'_>,
        label: Option<&str>,
        content_type: Option<&'a str>,
        url: Option<&'a str>,
        unknown: Unknown,
    ) -> PyResult<EncodingHints<'a>> {
        let resolved = label.and_then(|l| caller_label(py, l));
        if let (Some(l), None, Unknown::Refuse) = (label, resolved, unknown) {
            if !python_knows(py, l) {
                return Err(PyValueError::new_err(format!(
                    "frostwork: unknown encoding label {l:?} - pass a WHATWG charset label (e.g. \
                     'utf-8', 'windows-1252', 'shift_jis') or None to sniff from BOM/<meta>"
                )));
            }
        }
        let Html::Str(_) = self else {
            return Ok(EncodingHints { encoding: resolved, content_type, url });
        };
        match (label, resolved) {
            (Some(l), Some(name)) if name != "UTF-8" => Err(PyValueError::new_err(format!(
                "frostwork: html is already-decoded str (tokenized as UTF-8), but encoding={l:?} \
                 resolves to {name}, which would decode those bytes wrongly - pass the original bytes \
                 with the label, or drop the label."
            ))),
            _ => Ok(EncodingHints { encoding: Some("UTF-8"), content_type: None, url }),
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
    ///
    /// `parser` fixes the parse mode the plan answers in, once, here.
    #[new]
    #[pyo3(signature = (flat_queries, groups, first_only=None, *, parser="browser"))]
    fn new(
        flat_queries: Vec<Query>,
        groups: PyGroups,
        first_only: Option<Vec<bool>>,
        parser: &str,
    ) -> PyResult<Self> {
        let mode = parse_mode(parser)?;
        let first_only = first_only.unwrap_or_default();
        let options = CompileOptions { mode, first_only: &first_only };
        let inner = crate::Plan::compile_with(&flat_queries, &group_queries(groups), options);
        budget_error(inner.budget_usage())?;
        Ok(Plan { inner })
    }

    /// The parse mode this plan was compiled for: `"browser"` or `"lxml"`.
    #[getter]
    fn parser(&self) -> &'static str {
        self.inner.mode().as_str()
    }

    /// One run over `html` for every flat query, returning one value-column per query (query order).
    /// The GIL is released for the duration of the scan (`html` is an immutable buffer and
    /// the compiled plan is read-only), so concurrent extracts on a thread pool run in parallel.
    #[pyo3(signature = (html, encoding=None, *, content_type=None, url=None))]
    fn extract(
        &self,
        py: Python<'_>,
        html: Html<'_>,
        encoding: Option<&str>,
        content_type: Option<&str>,
        url: Option<&str>,
    ) -> PyResult<Vec<Vec<String>>> {
        let hints = html.hints(py, encoding, content_type, url, Unknown::Refuse)?;
        let bytes = html.as_bytes();
        Ok(py.detach(|| self.inner.extract_with(bytes, hints).0))
    }

    /// One run returning `(flat_columns, grouped)` — see the `extract_grouped` free function.
    /// Releases the GIL for the duration of the scan, like `extract`.
    #[pyo3(signature = (html, encoding=None, *, content_type=None, url=None))]
    #[allow(clippy::type_complexity)]
    fn extract_grouped(
        &self,
        py: Python<'_>,
        html: Html<'_>,
        encoding: Option<&str>,
        content_type: Option<&str>,
        url: Option<&str>,
    ) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>)> {
        let hints = html.hints(py, encoding, content_type, url, Unknown::Refuse)?;
        let bytes = html.as_bytes();
        Ok(py.detach(|| self.inner.extract_with(bytes, hints)))
    }

    /// `extract_grouped`, also returning the document's base URL and meta refresh, read as `navigation`
    /// reads them: `(flat_columns, grouped, (base_url, meta_refresh))`.
    #[pyo3(signature = (
        html, url, encoding=None, *, content_type=None, base_url=true, meta_refresh=true, scripting=true
    ))]
    #[allow(clippy::type_complexity, clippy::too_many_arguments)]
    fn extract_with_navigation(
        &self,
        py: Python<'_>,
        html: Html<'_>,
        url: &str,
        encoding: Option<&str>,
        content_type: Option<&str>,
        base_url: bool,
        meta_refresh: bool,
        scripting: bool,
    ) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>, PyNavigation)> {
        let hints = html.hints(py, encoding, content_type, Some(url), Unknown::Refuse)?;
        let bytes = html.as_bytes();
        let options = crate::NavOptions { base_url, meta_refresh, scripting };
        let (flat, grouped, nav) =
            py.detach(|| self.inner.extract_with_navigation_hints(bytes, hints, url, options));
        Ok((flat, grouped, py_navigation(nav)))
    }
}

/// `(base_url, meta_refresh)`, with the refresh delay as a float, which is how Scrapy compares it.
type PyNavigation = (Option<String>, Option<(f64, String)>);

fn py_navigation(nav: crate::Navigation) -> PyNavigation {
    (nav.base_url, nav.meta_refresh.map(|(delay, url)| (delay as f64, url)))
}

/// The document's base URL and meta refresh, as a browser reads them, in one pass over `html`:
/// `(base_url, meta_refresh)`. The pure-Python `frostwork.navigation` wraps this into a `Navigation`.
#[pyfunction]
#[pyo3(signature = (
    html, url, encoding=None, *, content_type=None, base_url=true, meta_refresh=true, scripting=true
))]
#[allow(clippy::too_many_arguments)]
fn navigation(
    py: Python<'_>,
    html: Html<'_>,
    url: &str,
    encoding: Option<&str>,
    content_type: Option<&str>,
    base_url: bool,
    meta_refresh: bool,
    scripting: bool,
) -> PyResult<PyNavigation> {
    let hints = html.hints(py, encoding, content_type, Some(url), Unknown::Ignore)?;
    let bytes = html.as_bytes();
    let options = crate::NavOptions { base_url, meta_refresh, scripting };
    Ok(py_navigation(py.detach(|| crate::navigation_with(bytes, hints, url, options))))
}

/// One run over `html` for every query, returning one value-column per query (in query order) — the exact
/// output of [`crate::extract`]. `html` is `bytes` (or a `bytes` subclass such as web-poet's
/// `HttpResponseBody`) or an already-decoded `str` — see [`Html`]; the pure-Python `frostwork.extract`
/// wrapper converts the remaining bytes-likes.
/// `encoding` is an optional label for an encoding the user forced, `content_type` the response's raw
/// `Content-Type` header value and `url` its URL; see [`Html::hints`] for how they rank. `parser` is the
/// parse mode, `"browser"` or `"lxml"`. Unsupported queries yield an empty column — there is no
/// fallback.
/// Raises `ValueError` if the schema exceeds the member/sibling-bit budget (a caller bug).
#[pyfunction]
#[pyo3(signature = (html, queries, encoding=None, *, content_type=None, url=None, parser="browser"))]
fn extract(
    py: Python<'_>,
    html: Html<'_>,
    queries: Vec<Query>,
    encoding: Option<&str>,
    content_type: Option<&str>,
    url: Option<&str>,
    parser: &str,
) -> PyResult<Vec<Vec<String>>> {
    let options = CompileOptions { mode: parse_mode(parser)?, first_only: &[] };
    check_budget(&queries, &[])?;
    let hints = html.hints(py, encoding, content_type, url, Unknown::Refuse)?;
    let bytes = html.as_bytes();
    Ok(py.detach(|| crate::Plan::compile_with(&queries, &[], options).extract_with(bytes, hints).0))
}

/// One run returning `(flat_columns, grouped)`. `groups` is a list of
/// `(container_selector, [(subfield_name, subfield_selector), ...])`; `grouped[g]` is that group's
/// rows in document order, each row a list of sub-field value-columns (`[group][row][subfield][value]`).
/// Sub-field names are carried by the caller (the pure-Python `Page`/`webpoet` layer); the engine keys
/// sub-columns positionally. Same scanning, tree and no-fallback semantics as `extract`.
#[pyfunction]
#[pyo3(signature = (html, flat_queries, groups, encoding=None, *, content_type=None, url=None, parser="browser"))]
#[allow(clippy::type_complexity, clippy::too_many_arguments)]
fn extract_grouped(
    py: Python<'_>,
    html: Html<'_>,
    flat_queries: Vec<Query>,
    groups: PyGroups,
    encoding: Option<&str>,
    content_type: Option<&str>,
    url: Option<&str>,
    parser: &str,
) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>)> {
    let options = CompileOptions { mode: parse_mode(parser)?, first_only: &[] };
    let gq = group_queries(groups);
    check_budget(&flat_queries, &gq)?;
    let hints = html.hints(py, encoding, content_type, url, Unknown::Refuse)?;
    let bytes = html.as_bytes();
    Ok(py.detach(|| crate::Plan::compile_with(&flat_queries, &gq, options).extract_with(bytes, hints)))
}

/// Resolve `label` the way every Python entry point does — WHATWG labels first, then Python's codec
/// set ([`caller_label`]) — returning the canonical encoding name (e.g. `"UTF-8"`, `"windows-1252"`) or
/// `None` if nothing names a WHATWG encoding.
#[pyfunction]
fn resolve_label(py: Python<'_>, label: &str) -> Option<&'static str> {
    caller_label(py, label)
}

/// The encoding `extract` would scan this document with, as a WHATWG canonical name, from the same
/// inputs and with the same errors ([`Html::hints`]). With `explain=True`, `(name, source)`, where
/// `source` names the rank that decided ([`EncodingSource::as_str`]), or `"str"` for already-decoded
/// text.
///
/// An already-decoded `str` is answered `"UTF-8"` without sniffing: those bytes ARE UTF-8, and a
/// `<meta charset>` surviving in a browser snapshot describes the document the browser already decoded,
/// not the string in hand. Reporting the meta would be reporting an encoding the caller must not apply.
#[pyfunction]
#[pyo3(signature = (html, encoding=None, *, content_type=None, url=None, explain=false))]
fn detect_encoding<'py>(
    py: Python<'py>,
    html: Html<'_>,
    encoding: Option<&str>,
    content_type: Option<&str>,
    url: Option<&str>,
    explain: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let hints = html.hints(py, encoding, content_type, url, Unknown::Ignore)?;
    let (name, source) = match html {
        Html::Str(_) => ("UTF-8", "str"),
        Html::Bytes(bytes) => {
            let (name, source): (&str, EncodingSource) = py.detach(|| crate::resolve_encoding(bytes, hints));
            (name, source.as_str())
        }
    };
    if explain {
        Ok((name, source).into_pyobject(py)?.into_any())
    } else {
        Ok(name.into_pyobject(py)?.into_any())
    }
}

/// The BOM step of [`detect_encoding`] alone: `(encoding, bom_length)`, or `None`.
#[pyfunction]
fn bom_encoding(body: &[u8]) -> Option<(&'static str, usize)> {
    crate::encoding::bom(body).map(|(enc, len)| (enc.name(), len))
}

/// The `<meta>`/XML-declaration prescan step of [`detect_encoding`] alone.
#[pyfunction]
fn declared_encoding(py: Python<'_>, body: &[u8]) -> Option<&'static str> {
    py.detach(|| crate::encoding::declaration(body).map(|(enc, _)| enc.name()))
}

/// The last step of [`detect_encoding`] alone: UTF-8 if `body` is valid UTF-8, else a guess.
#[pyfunction]
#[pyo3(signature = (body, *, url=None))]
fn autodetect_encoding(py: Python<'_>, body: &[u8], url: Option<&str>) -> &'static str {
    py.detach(|| crate::encoding::autodetect(body, url.and_then(crate::encoding::tld).as_deref()).0.name())
}

/// `body` decoded with the WHATWG decoder of the encoding the WHATWG label `label` names, BOM
/// included, with U+FFFD for invalid input. Raises `LookupError` for any other label.
#[pyfunction]
fn decode(py: Python<'_>, body: &[u8], label: &str) -> PyResult<String> {
    let Some(enc) = encoding_rs::Encoding::for_label(label.as_bytes()) else {
        return Err(PyLookupError::new_err(format!("unknown encoding: {label}")));
    };
    Ok(py.detach(|| enc.decode_without_bom_handling(body).0.into_owned()))
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
fn selector_terminals(queries: Vec<Query>) -> Vec<Option<&'static str>> {
    crate::selector_terminals(&queries)
}

/// Per query, `(pinned_tag, can_match_a_synthesized_frame)` — the matched-node identity the web-poet layer
/// needs before it re-parses an outer-HTML value; see [`crate::selector_node_identity`].
#[pyfunction]
fn selector_node_identity(queries: Vec<Query>) -> Vec<(Option<String>, bool)> {
    crate::selector_node_identity(&queries)
}

/// Audit a schema WITHOUT parsing any HTML: for each flat query, group container, and sub-field,
/// report whether the engine supports it (and, if not, a best-effort reason), plus the member /
/// sibling-bit budget usage against the limits. The no-fallback contract makes an unsupported selector
/// indistinguishable from a legitimately-empty field at runtime; this lets a caller check a schema up
/// front. Returns `(flat, groups, budget)` where `flat` is `[(supported, reason)]`, `groups` is
/// `[((c_supported, c_reason), [(supported, reason), ...])]`, and `budget` is
/// `(members, max_members, sib_bits, max_sib_bits)`. Support is per parse mode (`parser`). The
/// pure-Python `frostwork.check` wraps this into a named report; see `frostwork.page`.
#[pyfunction]
#[pyo3(signature = (flat_queries, groups, *, parser="browser"))]
#[allow(clippy::type_complexity)]
fn audit_schema(
    flat_queries: Vec<Query>,
    groups: PyGroups,
    parser: &str,
) -> PyResult<(
    Vec<(bool, Option<String>)>,
    Vec<((bool, Option<String>), Vec<(bool, Option<String>)>)>,
    (usize, usize, usize, usize),
)> {
    let a = crate::audit_schema_with(&flat_queries, &group_queries(groups), parse_mode(parser)?);
    let flat = a.flat.iter().map(support_tuple).collect();
    let grouped = a
        .groups
        .iter()
        .map(|g| (support_tuple(&g.container), g.subfields.iter().map(support_tuple).collect()))
        .collect();
    Ok((flat, grouped, (a.members, a.max_members, a.sib_bits, a.max_sib_bits)))
}

#[pymodule]
fn _frostwork(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__doc__", "Native Frostwork core: a streaming HTML parser for extraction.")?;
    m.add_function(wrap_pyfunction!(extract, m)?)?;
    m.add_function(wrap_pyfunction!(extract_grouped, m)?)?;
    m.add_function(wrap_pyfunction!(audit_schema, m)?)?;
    m.add_function(wrap_pyfunction!(selector_terminals, m)?)?;
    m.add_function(wrap_pyfunction!(selector_node_identity, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_label, m)?)?;
    m.add_function(wrap_pyfunction!(detect_encoding, m)?)?;
    m.add_function(wrap_pyfunction!(bom_encoding, m)?)?;
    m.add_function(wrap_pyfunction!(declared_encoding, m)?)?;
    m.add_function(wrap_pyfunction!(autodetect_encoding, m)?)?;
    m.add_function(wrap_pyfunction!(decode, m)?)?;
    m.add_function(wrap_pyfunction!(navigation, m)?)?;
    m.add_class::<Plan>()?;
    Ok(())
}
