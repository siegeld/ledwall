//! Named values pushed by marquee -- the data plane for `mode: program`.
//!
//! A program-mode panel composes its own pixels and never receives a frame. What
//! it needs instead is a handful of named values ("temp = 71"), which is a few
//! hundred bytes a second against the ~24 Mbit/s a streamed 256x192 panel eats
//! to show a static clock face.
//!
//! Two properties follow, and they are the point of the whole exercise:
//!
//!   * FAILURE BEHAVIOUR. A streamed sign goes black when marquee, the host or
//!     the network hiccups. This one keeps showing the last value it was told
//!     and can mark it stale, because the pixels are already on the panel.
//!   * IDLE COST. Nothing is sent while nothing changes.
//!
//! Fixed storage, no allocator: this is a no_std target and the table is written
//! from the HTTP handler inside the network ISR.

/// Values a program may bind to.
///
/// 24, not 16. `dashboard-home` binds twelve values from the bus plus four the
/// host derives, which is exactly 16 -- and `set()` ignores a key once the
/// table is full rather than evicting, so the SIXTEENTH key silently never
/// arrives. That key is whichever the pusher sends last, which was `hist`: the
/// power trace, i.e. the one value on the panel that is actually a picture.
/// Nothing reported it; the trace simply rendered empty.
///
/// The table is fixed storage on a board with megabytes spare, so the cliff is
/// not worth defending. A dashboard that needs more than this wants several
/// programs, but it should not fall off an edge eight values sooner than the
/// number in this constant suggests.
pub const MAX_VALUES: usize = 24;
const KEY_LEN: usize = 16;
/// 64, not 24: the power trace arrives as one ASCII byte per sample and the
/// host sends 60 of them. At 24 the value was silently truncated to its first
/// 23 samples -- and because the series is normalised to its own min..max
/// before sending, a truncated window is not a shortened trace but a wrong
/// one: the surviving samples were all near the top, so the sparkline rendered
/// as a solid brass slab with no shape at all.
const VAL_LEN: usize = 64;

#[derive(Clone, Copy)]
pub struct Value {
    key: [u8; KEY_LEN],
    key_len: u8,
    val: [u8; VAL_LEN],
    val_len: u8,
    /// TIME_MS of the last write. 0 means never written.
    pub updated_ms: i64,
}

impl Value {
    const fn empty() -> Self {
        Self { key: [0; KEY_LEN], key_len: 0, val: [0; VAL_LEN], val_len: 0, updated_ms: 0 }
    }
    /// SAFETY, for both of these: every byte in `key`/`val` was copied from a
    /// `&str` in `set()`, so the stored bytes are valid UTF-8 by construction
    /// and there is nothing for a checked conversion to discover.
    ///
    /// That matters because these are the hottest functions a program has.
    /// `from_utf8` VALIDATES -- a branchy walk of every byte -- and `get()`
    /// called `key()` once per occupied slot, so a single `ctx.val()` validated
    /// up to 16 keys and then the value it found. On a core with no D-cache
    /// each of those bytes is its own load. Profiled on dashboard-home: the
    /// checked version cost ~3900 cycles per lookup, against 10 cycles for a
    /// framebuffer store.
    pub fn key(&self) -> &str {
        unsafe { core::str::from_utf8_unchecked(&self.key[..self.key_len as usize]) }
    }
    pub fn value(&self) -> &str {
        unsafe { core::str::from_utf8_unchecked(&self.val[..self.val_len as usize]) }
    }
    /// Key comparison without building a `&str` at all -- a length test rejects
    /// almost every slot in one compare.
    #[inline(always)]
    fn key_is(&self, k: &[u8]) -> bool {
        self.key_len as usize == k.len() && &self.key[..k.len()] == k
    }
}

pub struct ValueTable {
    slots: [Value; MAX_VALUES],
    used: usize,
    pub writes: u32,
    /// TIME_MS of the most recent push of any key -- drives the staleness marker.
    pub last_push_ms: i64,
}

impl ValueTable {
    pub const fn new() -> Self {
        Self { slots: [Value::empty(); MAX_VALUES], used: 0, writes: 0, last_push_ms: 0 }
    }

    /// Insert or update. Silently ignored once full rather than evicting: a
    /// program binds to a fixed set of keys, so dropping the 17th is far less
    /// surprising than a widget's key vanishing because something else pushed.
    pub fn set(&mut self, key: &str, val: &str, now_ms: i64) {
        self.writes += 1;
        self.last_push_ms = now_ms;
        let kb0 = key.as_bytes();
        if let Some(i) = (0..self.used).find(|&i| self.slots[i].key_is(kb0)) {
            Self::store_val(&mut self.slots[i], val, now_ms);
            return;
        }
        if self.used >= MAX_VALUES || key.is_empty() || key.len() > KEY_LEN {
            return;
        }
        let s = &mut self.slots[self.used];
        let kb = key.as_bytes();
        s.key[..kb.len()].copy_from_slice(kb);
        s.key_len = kb.len() as u8;
        Self::store_val(s, val, now_ms);
        self.used += 1;
    }

    fn store_val(s: &mut Value, val: &str, now_ms: i64) {
        let vb = val.as_bytes();
        let n = vb.len().min(VAL_LEN);
        s.val[..n].copy_from_slice(&vb[..n]);
        s.val_len = n as u8;
        s.updated_ms = now_ms;
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        let k = key.as_bytes();
        (0..self.used).map(|i| &self.slots[i]).find(|v| v.key_is(k))
    }

    /// The value as text, or `-` when it has never arrived. A program must never
    /// have to decide what absent looks like.
    pub fn text(&self, key: &str) -> &str {
        match self.get(key) {
            Some(v) if v.val_len > 0 => v.value(),
            _ => "-",
        }
    }

    pub fn number(&self, key: &str) -> Option<f32> {
        self.get(key).and_then(|v| v.value().parse::<f32>().ok())
    }

    pub fn iter(&self) -> impl Iterator<Item = &Value> {
        (0..self.used).map(move |i| &self.slots[i])
    }

    pub fn len(&self) -> usize {
        self.used
    }

    /// Nothing pushed for this long means the feed is gone; a program should say
    /// so rather than presenting a stale number as current.
    pub fn stale_for_ms(&self, now_ms: i64) -> i64 {
        if self.last_push_ms == 0 { i64::MAX } else { now_ms.saturating_sub(self.last_push_ms) }
    }
}
