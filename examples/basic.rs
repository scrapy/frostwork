use frostwork::Page;

fn main() {
    let page = Page::new()
        .field("title", "h1::text")
        .field("price", ".price::text")
        .field_all("images", "img::attr(src)");

    // Page caches its compiled plan after this first call and reuses it for later responses.
    let item = page.extract(
        b"<main><h1>Widget</h1><span class=price>$9</span>\
          <img src=/a.png><img src=/b.png></main>",
    );
    println!("{}", item.to_json());
}
