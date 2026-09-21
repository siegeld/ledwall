//! HTTP request/response types for the network module.

// ── Request Parser ──────────────────────────────────────────────

const MAX_REQUEST: usize = 512;

pub enum Method {
    Get,
    Post,
    Unknown,
}

enum ParseState {
    ReadingHeaders,
    ReadingBody { content_length: usize, body_start: usize },
    Complete,
}

pub struct HttpRequest {
    buf: [u8; MAX_REQUEST],
    len: usize,
    state: ParseState,
}

impl HttpRequest {
    pub fn new() -> Self {
        Self {
            buf: [0; MAX_REQUEST],
            len: 0,
            state: ParseState::ReadingHeaders,
        }
    }

    pub fn reset(&mut self) {
        self.len = 0;
        self.state = ParseState::ReadingHeaders;
    }

    pub fn is_complete(&self) -> bool {
        matches!(self.state, ParseState::Complete)
    }

    /// Feed received bytes. Returns `true` when the full request is available.
    pub fn feed(&mut self, data: &[u8]) -> bool {
        let space = MAX_REQUEST - self.len;
        let n = data.len().min(space);
        self.buf[self.len..self.len + n].copy_from_slice(&data[..n]);
        self.len += n;

        match self.state {
            ParseState::ReadingHeaders => {
                if let Some(end) = find_header_end(&self.buf[..self.len]) {
                    let body_start = end + 4;
                    let cl = parse_content_length(&self.buf[..end]);
                    if cl > 0 && self.len < body_start + cl {
                        self.state = ParseState::ReadingBody { content_length: cl, body_start };
                    } else {
                        self.state = ParseState::Complete;
                    }
                } else if self.len >= MAX_REQUEST {
                    self.state = ParseState::Complete;
                }
            }
            ParseState::ReadingBody { content_length, body_start } => {
                if self.len >= body_start + content_length || self.len >= MAX_REQUEST {
                    self.state = ParseState::Complete;
                }
            }
            ParseState::Complete => {}
        }
        self.is_complete()
    }

    pub fn method(&self) -> Method {
        if self.len >= 4 && &self.buf[..4] == b"GET " {
            Method::Get
        } else if self.len >= 5 && &self.buf[..5] == b"POST " {
            Method::Post
        } else {
            Method::Unknown
        }
    }

    pub fn path(&self) -> &str {
        let start = match self.method() {
            Method::Get => 4,
            Method::Post => 5,
            Method::Unknown => return "/",
        };
        let mut end = start;
        while end < self.len && self.buf[end] != b' ' && self.buf[end] != b'\r' {
            end += 1;
        }
        core::str::from_utf8(&self.buf[start..end]).unwrap_or("/")
    }

    pub fn body_str(&self) -> &str {
        if let Some(end) = find_header_end(&self.buf[..self.len]) {
            let body_start = end + 4;
            if body_start < self.len {
                return core::str::from_utf8(&self.buf[body_start..self.len]).unwrap_or("");
            }
        }
        ""
    }
}

// ── Response Writer ─────────────────────────────────────────────

pub struct HttpResponse {
    /// 8 KiB, not 6: the status page renders ~137 fields into one buffer and
    /// every write goes through `.ok()`, so an overflow does not fail -- it
    /// silently truncates the HTML mid-tag and the page just renders wrong.
    /// Literals alone are ~3.6 KiB; the counters grow with uptime. Two
    /// instances, so this costs 4 KiB of a board that has megabytes spare.
    pub data: heapless::Vec<u8, 8192>,
}

impl HttpResponse {
    pub fn new() -> Self {
        Self { data: heapless::Vec::new() }
    }
}

impl core::fmt::Write for HttpResponse {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        self.data.extend_from_slice(s.as_bytes()).map_err(|_| core::fmt::Error)
    }
}

// ── Helpers ─────────────────────────────────────────────────────

fn find_header_end(buf: &[u8]) -> Option<usize> {
    if buf.len() < 4 { return None; }
    for i in 0..buf.len() - 3 {
        if &buf[i..i + 4] == b"\r\n\r\n" {
            return Some(i);
        }
    }
    None
}

fn parse_content_length(headers: &[u8]) -> usize {
    for pattern in &[b"Content-Length: " as &[u8], b"content-length: "] {
        if let Some(pos) = find_bytes(headers, pattern) {
            let start = pos + pattern.len();
            let mut end = start;
            while end < headers.len() && headers[end] >= b'0' && headers[end] <= b'9' {
                end += 1;
            }
            if let Ok(s) = core::str::from_utf8(&headers[start..end]) {
                return parse_usize(s).unwrap_or(0);
            }
        }
    }
    0
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() { return None; }
    for i in 0..=haystack.len() - needle.len() {
        if &haystack[i..i + needle.len()] == needle {
            return Some(i);
        }
    }
    None
}

fn parse_usize(s: &str) -> Result<usize, ()> {
    let mut r: usize = 0;
    if s.is_empty() { return Err(()); }
    for b in s.bytes() {
        if b < b'0' || b > b'9' { return Err(()); }
        r = r.checked_mul(10).ok_or(())?.checked_add((b - b'0') as usize).ok_or(())?;
    }
    Ok(r)
}
