// gs-set-cache.js — per-set offline data cache (IndexedDB), MTG/YGO only.
//
// Net-new infrastructure: no other part of this app uses IndexedDB (Card
// Show Mode's existing Pokémon flow caches whole-era card DATA only in the
// in-memory _gsLocalIndexCache var, plus images in the SW's Cache Storage
// gs-images-v2 store). Kept in its own file, not inlined in search.html, on
// purpose -- offline is the hardest place to debug, and this is the one
// piece with no prior art in this codebase to fall back on if something
// goes wrong. Every method is a thin, independently testable wrapper; no
// UI logic lives here.
//
// One database (gs-set-cache-v1), one object store (sets), keyed by
// "<game>::<set_id>" so MTG and YGO sets never collide. Each record:
//   { key, game, set_id, name, total, cards: [...], saved_at }
// `cards` is the enriched /api/sets/<set_id>/cards response's card array
// (sku/name/card_number/img_url/price/currency) -- exactly what offline
// search needs, nothing more.
(function () {
  'use strict';

  var DB_NAME = 'gs-set-cache-v1';
  var STORE = 'sets';
  var DB_VERSION = 1;
  var _dbPromise = null;

  function _openDb() {
    if (_dbPromise) return _dbPromise;
    _dbPromise = new Promise(function (resolve, reject) {
      if (!window.indexedDB) {
        reject(new Error('indexedDB unavailable'));
        return;
      }
      var req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          var store = db.createObjectStore(STORE, { keyPath: 'key' });
          store.createIndex('game', 'game', { unique: false });
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error || new Error('indexedDB open failed')); };
    });
    return _dbPromise;
  }

  function _key(game, setId) { return game + '::' + setId; }

  // Saves one set's card data. Overwrites any existing entry for the same
  // (game, set_id) -- re-downloading a set is expected to refresh it.
  function saveSet(game, setId, setName, total, cards) {
    return _openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put({
          key: _key(game, setId),
          game: game,
          set_id: setId,
          name: setName,
          total: total,
          cards: cards,
          saved_at: Date.now(),
        });
        tx.oncomplete = function () { resolve(true); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function getSet(game, setId) {
    return _openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, 'readonly');
        var req = tx.objectStore(STORE).get(_key(game, setId));
        req.onsuccess = function () { resolve(req.result || null); };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  function removeSet(game, setId) {
    return _openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).delete(_key(game, setId));
        tx.oncomplete = function () { resolve(true); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  // Every downloaded set for one game -- the building block offline search
  // merges over. Never touches anything beyond what's actually downloaded,
  // by construction (there is no "list all sets" fallback here).
  function getAllDownloadedSets(game) {
    return _openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, 'readonly');
        var idx = tx.objectStore(STORE).index('game');
        var req = idx.getAll(IDBKeyRange.only(game));
        req.onsuccess = function () { resolve(req.result || []); };
        req.onerror = function () { reject(req.error); };
      });
    });
  }

  // Prefix-match search (name or card_number) merged across every
  // downloaded set for this game -- the offline equivalent of
  // /api/card-search, deliberately scoped to what's on the device rather
  // than the whole game (there is no server to ask offline; see the
  // 2026-08-25 recon on why this is a real, disclosed narrowing rather
  // than an oversight). Mirrors search.html's own _gsSearchOffline
  // prefix-match semantics for consistency.
  function searchDownloaded(game, rawQuery, limit) {
    limit = limit || 30;
    var q = (rawQuery || '').trim().toLowerCase();
    return getAllDownloadedSets(game).then(function (sets) {
      var results = [];
      for (var i = 0; i < sets.length && results.length < limit; i++) {
        var cards = sets[i].cards || [];
        for (var j = 0; j < cards.length && results.length < limit; j++) {
          var c = cards[j];
          var num = (c.card_number || '').toLowerCase();
          var name = (c.name || '').toLowerCase();
          if (num.indexOf(q) === 0 || name.indexOf(q) === 0) {
            results.push(c);
          }
        }
      }
      return results;
    });
  }

  window.GSSetCache = {
    saveSet: saveSet,
    getSet: getSet,
    removeSet: removeSet,
    getAllDownloadedSets: getAllDownloadedSets,
    searchDownloaded: searchDownloaded,
  };
})();
