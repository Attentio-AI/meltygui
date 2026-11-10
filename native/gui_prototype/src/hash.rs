//! Small non-cryptographic hasher for runtime-local integer and field-name keys.
//! Graph, geometry and texture tables are looked up many times per view; the
//! standard SipHash dominated those lookups. Keys are never attacker supplied.
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};

const SEED: u64 = 0x51_7c_c1_b7_27_22_0a_95;

#[derive(Default, Clone, Copy)]
pub struct IdHasher(u64);
impl IdHasher {
    #[inline]
    fn add(&mut self, value: u64) {
        self.0 = (self.0.rotate_left(5) ^ value).wrapping_mul(SEED);
    }
}
impl Hasher for IdHasher {
    #[inline]
    fn finish(&self) -> u64 {
        self.0
    }
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut chunks = bytes.chunks_exact(8);
        for chunk in &mut chunks {
            self.add(u64::from_ne_bytes(chunk.try_into().unwrap()));
        }
        let rest = chunks.remainder();
        if !rest.is_empty() {
            let mut tail = [0_u8; 8];
            tail[..rest.len()].copy_from_slice(rest);
            self.add(u64::from_ne_bytes(tail));
        }
    }
    #[inline]
    fn write_u8(&mut self, value: u8) {
        self.add(value as u64);
    }
    #[inline]
    fn write_u32(&mut self, value: u32) {
        self.add(value as u64);
    }
    #[inline]
    fn write_u64(&mut self, value: u64) {
        self.add(value);
    }
    #[inline]
    fn write_i64(&mut self, value: i64) {
        self.add(value as u64);
    }
    #[inline]
    fn write_usize(&mut self, value: usize) {
        self.add(value as u64);
    }
    #[inline]
    fn write_u128(&mut self, value: u128) {
        self.add(value as u64);
        self.add((value >> 64) as u64);
    }
}

pub type Map<K, V> = HashMap<K, V, BuildHasherDefault<IdHasher>>;
pub type Set<K> = HashSet<K, BuildHasherDefault<IdHasher>>;
