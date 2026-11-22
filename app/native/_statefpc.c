/* C version of statefp.Fingerprinter.fp: the same walk, the same tuples, the same hashes.
   Every branch builds the very tuple that the Python code builds and hashes it with
   PyObject_Hash, so a value is the same as the Python one by construction. The branches that
   are not written here (builtins, patterns, subclasses of the containers, partials, sqlglot
   objects, wrappers, repr) are left to the Python method Fingerprinter._fp_rest. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stddef.h>

static PyObject *n_str, *n_int, *n_float, *n_bool, *n_bytes, *n_complex, *n_none;
static PyObject *t_dict, *t_list, *t_tuple, *t_set, *t_frozenset, *t_staticmethod, *t_classmethod;
static PyObject *k_cycle, *k_m, *k_d, *k_tc, *k_s, *k_E, *k_C, *k_M, *k_F, *k_prop, *k_meth, *empty_cell;
static PyObject *a_name, *a_module, *a_qualname, *a_value, *a_func, *a_self, *a_fget, *a_fset, *a_fdel,
    *a_ename, *a_code, *a_defaults, *a_kwdefaults, *a_closure, *a_cell_contents, *m_code, *m_rest, *m_items;
static PyObject *enum_class = NULL, *marshal_dumps = NULL, *two = NULL, *prim_dict = NULL;
static PyObject *m_head, *m_fp, *m_fp_plain;

typedef struct {
    PyObject_HEAD
    PyObject *owner;  /* weak reference to the Fingerprinter (it holds this object) */
    PyObject *memo, *busy, *code_memo, *base, *code_alias, *code_objects;
    /* memo and busy of Fingerprinter.fp, keyed by the object's address (its id): a value
       (the hash, an int) or BUSY; key 0 is an empty slot, a NULL value a removed entry */
    struct slot { uintptr_t key; PyObject *val; } *tab;
    size_t mask, used;
} FPObject;

#define BUSY ((PyObject *)1)

static inline size_t slot_hash(uintptr_t p)
{
    uint64_t h = (uint64_t)p >> 4;
    h ^= h >> 17;
    h *= 0x9E3779B97F4A7C15ULL;
    h ^= h >> 29;
    return (size_t)h;
}

/* the slot that holds key (busy or a value), or NULL */
static inline struct slot *tab_find(FPObject *self, uintptr_t key)
{
    size_t i = slot_hash(key) & self->mask;
    for (;;) {
        struct slot *s = &self->tab[i];
        if (s->key == key && s->val != NULL) return s;
        if (s->key == 0) return NULL;
        i = (i + 1) & self->mask;
    }
}

static int tab_grow(FPObject *self)
{
    size_t n = (self->mask + 1) * 2, live = 0;
    struct slot *t = PyMem_Calloc(n, sizeof(struct slot));
    if (!t) { PyErr_NoMemory(); return -1; }
    for (size_t j = 0; j <= self->mask; j++) {
        struct slot *s = &self->tab[j];
        if (s->key == 0 || s->val == NULL) continue;
        size_t i = slot_hash(s->key) & (n - 1);
        while (t[i].key != 0) i = (i + 1) & (n - 1);
        t[i] = *s;
        live++;
    }
    PyMem_Free(self->tab);
    self->tab = t;
    self->mask = n - 1;
    self->used = live;
    return 0;
}

/* sets key to val (BUSY or a new reference, which the table takes); the key is not live */
static int tab_put(FPObject *self, uintptr_t key, PyObject *val)
{
    if ((self->used + 1) * 2 > self->mask + 1 && tab_grow(self) < 0) {
        if (val != BUSY) Py_DECREF(val);
        return -1;
    }
    size_t i = slot_hash(key) & self->mask;
    struct slot *tomb = NULL;
    for (;;) {
        struct slot *s = &self->tab[i];
        if (s->key == 0) break;
        if (s->val == NULL && (tomb == NULL || s->key == key)) tomb = s;
        i = (i + 1) & self->mask;
    }
    struct slot *s = tomb ? tomb : &self->tab[i];
    if (!tomb) self->used++;
    s->key = key;
    s->val = val;
    return 0;
}

/* removes key (busy) */
static inline void tab_drop(FPObject *self, uintptr_t key)
{
    struct slot *s = tab_find(self, key);
    if (s) {
        if (s->val != BUSY) Py_DECREF(s->val);
        s->val = NULL;
    }
}

static PyObject *fp(FPObject *self, PyObject *v, long depth);
static PyObject *fp_np(FPObject *self, PyObject *v, long depth);

static inline PyObject *prim_name(PyTypeObject *t)
{
    if (t == &PyUnicode_Type) return n_str;
    if (t == &PyLong_Type) return n_int;
    if (t == &PyFloat_Type) return n_float;
    if (t == &PyBool_Type) return n_bool;
    if (t == &PyBytes_Type) return n_bytes;
    if (t == &PyComplex_Type) return n_complex;
    if (t == Py_TYPE(Py_None)) return n_none;
    return NULL;
}

/* 1 when t's metaclass compares and hashes t as type does (by identity): its tp_richcompare and
   tp_hash are type's (the slots that ==, `in` and dict lookups of Python call). Then t == X,
   t in (X, Y), d.get(t) and hash(t) give what the pointer tests in this file give. Else (a
   metaclass with its own __eq__ or __hash__) the Python code runs those tests. */
static inline int plain_meta(PyTypeObject *t)
{
    PyTypeObject *m = Py_TYPE(t);
    return m == &PyType_Type || (m->tp_richcompare == PyType_Type.tp_richcompare && m->tp_hash == PyType_Type.tp_hash);
}

/* _PRIM_NAME.get(t) as Python evaluates it: a new reference, or NULL when absent (*err = 1 on
   an exception, which the lookup of a type with such a metaclass can raise) */
static inline PyObject *prim_get(PyTypeObject *t, int *err)
{
    if (plain_meta(t)) {
        PyObject *n = prim_name(t);
        return n ? Py_NewRef(n) : NULL;
    }
    PyObject *n = NULL;
    Py_INCREF(t);
    if (PyDict_GetItemRef(prim_dict, (PyObject *)t, &n) < 0) *err = 1;
    Py_DECREF(t);
    return n;
}

static PyObject *get_owner(FPObject *self)
{
    PyObject *o = NULL;
    if (PyWeakref_GetRef(self->owner, &o) < 0) return NULL;
    if (o == NULL) PyErr_SetString(PyExc_ReferenceError, "Fingerprinter is gone");
    return o;
}

/* memo.get(id(x)) of _seq / _dict: the stored value (a new reference), or NULL (also when x is busy) */
static inline PyObject *memo_get(FPObject *self, PyObject *x)
{
    struct slot *s = tab_find(self, (uintptr_t)x);
    if (s != NULL && s->val != BUSY) return Py_NewRef(s->val);
    return NULL;
}

/* one item of _seq: (n, x) of a primitive (_PRIM_NAME.get(type(x))), else memo.get(id(x)), else
   fp(x, d) (which looks the type up again, as the Python fp does) */
static PyObject *seq_item(FPObject *self, PyObject *x, long d)
{
    int err = 0;
    PyObject *n = prim_get(Py_TYPE(x), &err);
    if (n) {
        PyObject *r = PyTuple_Pack(2, n, x);
        Py_DECREF(n);
        return r;
    }
    if (err) return NULL;
    PyObject *r = memo_get(self, x);
    return r ? r : fp(self, x, d);
}

/* _seq: fp(x, d) for every item of v, in iteration order, as a list */
static PyObject *seq(FPObject *self, PyObject *v, long d)
{
    PyObject *out = PyList_New(0);
    if (!out) return NULL;
    if (PyList_CheckExact(v) || PyTuple_CheckExact(v)) {
        /* the index loop of the list / tuple iterator (the size is read at each step) */
        for (Py_ssize_t i = 0; i < Py_SIZE(v); i++) {
            PyObject *x = PyList_CheckExact(v) ? PyList_GET_ITEM(v, i) : PyTuple_GET_ITEM(v, i);
            Py_INCREF(x);
            PyObject *r = seq_item(self, x, d);
            Py_DECREF(x);
            if (!r || PyList_Append(out, r) < 0) { Py_XDECREF(r); Py_DECREF(out); return NULL; }
            Py_DECREF(r);
        }
        return out;
    }
    PyObject *it = PyObject_GetIter(v);
    if (!it) { Py_DECREF(out); return NULL; }
    PyObject *x;
    while ((x = PyIter_Next(it)) != NULL) {
        PyObject *r = seq_item(self, x, d);
        Py_DECREF(x);
        if (!r || PyList_Append(out, r) < 0) { Py_XDECREF(r); Py_DECREF(it); Py_DECREF(out); return NULL; }
        Py_DECREF(r);
    }
    Py_DECREF(it);
    if (PyErr_Occurred()) { Py_DECREF(out); return NULL; }
    return out;
}

/* self.fp(getattr(v, attr), d) */
static PyObject *fp_attr(FPObject *self, PyObject *v, PyObject *attr, long d)
{
    PyObject *x = PyObject_GetAttr(v, attr);
    if (!x) return NULL;
    PyObject *r = fp(self, x, d);
    Py_DECREF(x);
    return r;
}

static PyObject *pack_steal(Py_ssize_t n, PyObject **items)
{
    for (Py_ssize_t i = 0; i < n; i++) if (!items[i]) { for (Py_ssize_t j = 0; j < n; j++) Py_XDECREF(items[j]); return NULL; }
    PyObject *t = PyTuple_New(n);
    if (!t) { for (Py_ssize_t j = 0; j < n; j++) Py_DECREF(items[j]); return NULL; }
    for (Py_ssize_t i = 0; i < n; i++) PyTuple_SET_ITEM(t, i, items[i]);
    return t;
}


/* ---- hashes of the tuples that fp builds, computed without building them ----
   tuplehash of CPython 3.13 (Objects/tupleobject.c), checked against PyObject_Hash by setup()
   on sample tuples; when it differs, every tuple is built as before (hash_ok = 0). Only
   items whose hash cannot fail are hashed this way: a primitive pair (n, x) of exact
   builtin types, an exact int (a memo value); any other item builds the real tuple. */
#define XXPRIME_1 ((Py_uhash_t)11400714785074694791ULL)
#define XXPRIME_2 ((Py_uhash_t)14029467366897019727ULL)
#define XXPRIME_5 ((Py_uhash_t)2870177450012600261ULL)
#define XXROTATE(x) ((x << 31) | (x >> 33))
static int hash_ok = 0;
static Py_hash_t empty_hash = -1;  /* hash(("empty",)) */

static inline Py_uhash_t xx_step(Py_uhash_t acc, Py_uhash_t lane)
{
    acc += lane * XXPRIME_2;
    acc = XXROTATE(acc);
    acc *= XXPRIME_1;
    return acc;
}

static inline Py_hash_t xx_end(Py_uhash_t acc, Py_ssize_t len)
{
    acc += (Py_uhash_t)len ^ (XXPRIME_5 ^ 3527539UL);
    if (acc == (Py_uhash_t)-1) return 1546275796;
    return (Py_hash_t)acc;
}

static inline Py_hash_t xx_pair(Py_hash_t a, Py_hash_t b)
{
    return xx_end(xx_step(xx_step(XXPRIME_5, (Py_uhash_t)a), (Py_uhash_t)b), 2);
}

/* an fp result held without its tuple: n != NULL: the pair (n, obj) of a primitive obj;
   else obj is the result itself. Both are strong references. */
typedef struct { PyObject *obj, *n; } Item;
typedef struct { Item *v; Py_ssize_t len, cap; } Items;

static void items_clear(Items *it)
{
    for (Py_ssize_t i = 0; i < it->len; i++) { Py_XDECREF(it->v[i].obj); Py_XDECREF(it->v[i].n); }
    PyMem_Free(it->v);
    it->v = NULL; it->len = it->cap = 0;
}

static int items_push(Items *it, PyObject *obj, PyObject *n)  /* steals obj, borrows n */
{
    if (it->len == it->cap) {
        Py_ssize_t cap = it->cap ? it->cap * 2 : 16;
        Item *v = PyMem_Realloc(it->v, cap * sizeof(Item));
        if (!v) { Py_DECREF(obj); PyErr_NoMemory(); return -1; }
        it->v = v; it->cap = cap;
    }
    it->v[it->len].obj = obj;
    it->v[it->len].n = n ? Py_NewRef(n) : NULL;
    it->len++;
    return 0;
}

/* the hash of the item's value, or 0 in *ok when it is not one of the infallible kinds */
static inline Py_hash_t item_hash(Item *x, int *ok)
{
    if (x->n) {
        /* (only an object of an exact primitive type: its hash cannot fail and runs no code) */
        if (prim_name(Py_TYPE(x->obj)) == NULL || !PyUnicode_CheckExact(x->n)) { *ok = 0; return 0; }
        return xx_pair(PyObject_Hash(x->n), PyObject_Hash(x->obj));
    }
    if (PyLong_CheckExact(x->obj)) return PyObject_Hash(x->obj);
    *ok = 0;
    return 0;
}

/* the value of the item (the pair of a primitive is built) */
static PyObject *item_value(Item *x)
{
    if (x->n) return PyTuple_Pack(2, x->n, x->obj);
    return Py_NewRef(x->obj);
}

/* fp(x, d) as an item: a primitive is not looked up (fp gives (n, x) for it, with no other step) */
static int push_fp(FPObject *self, Items *it, PyObject *x, long d)
{
    int err = 0;
    PyObject *n = prim_get(Py_TYPE(x), &err);
    if (n) {
        int e = items_push(it, Py_NewRef(x), n);
        Py_DECREF(n);
        return e;
    }
    if (err) return -1;
    PyObject *r = memo_get(self, x);
    if (!r) r = fp(self, x, d);
    if (!r) return -1;
    return items_push(it, r, NULL);
}

static int hash_selftest(void)
{
    /* tuples of the kinds fp hashes: (), (1,), ("str", "x"), ("d", (((n, k), 5), ...)), big and negative ints */
    PyObject *vals[8];
    vals[0] = PyLong_FromLong(-1);
    vals[1] = PyLong_FromLongLong(-4611686018427387904LL);
    vals[2] = PyLong_FromUnsignedLongLong(18446744073709551615ULL);
    vals[3] = PyUnicode_FromString("str");
    vals[4] = PyUnicode_FromString("x");
    vals[5] = PyBytes_FromString("abc");
    vals[6] = PyFloat_FromDouble(1.5);
    vals[7] = PyLong_FromLong(0);
    for (int i = 0; i < 8; i++) if (!vals[i]) { for (int j = 0; j < 8; j++) Py_XDECREF(vals[j]); return -1; }
    int ok = 1;
    for (int len = 0; len <= 8 && ok; len++) {
        PyObject *t = PyTuple_New(len);
        if (!t) { ok = -1; break; }
        Py_uhash_t acc = XXPRIME_5;
        for (int i = 0; i < len; i++) {
            PyTuple_SET_ITEM(t, i, Py_NewRef(vals[(i * 3 + len) % 8]));
            acc = xx_step(acc, (Py_uhash_t)PyObject_Hash(vals[(i * 3 + len) % 8]));
        }
        Py_hash_t want = PyObject_Hash(t);
        if (want == -1 || want != xx_end(acc, len)) ok = 0;
        PyObject *p = PyTuple_Pack(2, vals[3], t);
        if (!p) { Py_DECREF(t); ok = -1; break; }
        want = PyObject_Hash(p);
        if (want == -1 || want != xx_pair(PyObject_Hash(vals[3]), PyObject_Hash(t))) ok = 0;
        Py_DECREF(p);
        Py_DECREF(t);
    }
    for (int j = 0; j < 8; j++) Py_DECREF(vals[j]);
    if (ok < 0) return -1;
    empty_hash = PyObject_Hash(empty_cell);
    if (empty_hash == -1) return -1;
    hash_ok = ok;
    return 0;
}


#define HASHED ((PyObject *)2)  /* fp_inner's result when *h holds the hash of the tuple it did not build */

/* _dict: ("d", tuple(pairs)) or ("d", frozenset(pairs)); HASHED with *h set when the tuple
   one could be hashed from its items (h == NULL: always the tuple) */
static PyObject *cdict(FPObject *self, PyObject *v, long depth, Py_hash_t *h)
{
    long d = depth + 1;
    int classes = 0;
    Items its = {NULL, 0, 0};
    PyObject *view = NULL, *it = NULL, *item, *res = NULL;
    view = PyObject_CallMethodNoArgs(v, m_items);
    if (!view) goto done;
    it = PyObject_GetIter(view);
    if (!it) goto done;
    while ((item = PyIter_Next(it)) != NULL) {
        if (!PyTuple_Check(item) || PyTuple_GET_SIZE(item) != 2) {
            Py_DECREF(item);
            PyErr_SetString(PyExc_ValueError, "items() gave no pair");
            goto done;
        }
        PyObject *k = PyTuple_GET_ITEM(item, 0), *x = PyTuple_GET_ITEM(item, 1);
        Py_INCREF(k); Py_INCREF(x); Py_DECREF(item);
        int e, err = 0;
        /* as _dict: n = prim(type(k)); then isinstance(k, type) and fp(k, d) for another key */
        PyObject *nk = prim_get(Py_TYPE(k), &err);
        if (err) { Py_DECREF(k); Py_DECREF(x); goto done; }
        if (nk) {
            e = items_push(&its, Py_NewRef(k), nk);
            Py_DECREF(nk);
        } else {
            if (!classes) {
                int r = PyObject_IsInstance(k, (PyObject *)&PyType_Type);
                if (r < 0) { Py_DECREF(k); Py_DECREF(x); goto done; }
                if (r) classes = 1;
            }
            PyObject *rk = memo_get(self, k);
            if (!rk) rk = fp(self, k, d);
            e = rk ? items_push(&its, rk, NULL) : -1;
        }
        Py_DECREF(k);
        if (e < 0) { Py_DECREF(x); goto done; }
        e = push_fp(self, &its, x, d);
        Py_DECREF(x);
        if (e < 0) goto done;
    }
    if (PyErr_Occurred()) goto done;
    Py_ssize_t npairs = its.len / 2;
    if (!classes && hash_ok && h != NULL) {
        int ok = 1;
        Py_uhash_t acc = XXPRIME_5;
        for (Py_ssize_t i = 0; i < npairs && ok; i++) {
            Py_hash_t a = item_hash(&its.v[2 * i], &ok);
            Py_hash_t b = item_hash(&its.v[2 * i + 1], &ok);
            acc = xx_step(acc, (Py_uhash_t)xx_pair(a, b));
        }
        if (ok) {
            *h = xx_pair(PyObject_Hash(k_d), xx_end(acc, npairs));
            res = HASHED;
            goto done;
        }
    }
    {
        PyObject *pairs = PyList_New(npairs);
        if (!pairs) goto done;
        for (Py_ssize_t i = 0; i < npairs; i++) {
            PyObject *a = item_value(&its.v[2 * i]);
            PyObject *b = a ? item_value(&its.v[2 * i + 1]) : NULL;
            PyObject *p = b ? PyTuple_Pack(2, a, b) : NULL;
            Py_XDECREF(a); Py_XDECREF(b);
            if (!p) { Py_DECREF(pairs); goto done; }
            PyList_SET_ITEM(pairs, i, p);
        }
        PyObject *c = classes ? PyFrozenSet_New(pairs) : PyList_AsTuple(pairs);
        Py_DECREF(pairs);
        if (c) { res = PyTuple_Pack(2, k_d, c); Py_DECREF(c); }
    }
done:
    items_clear(&its);
    Py_XDECREF(it); Py_XDECREF(view);
    return res;
}

/* (key, tuple(fp(x, d) for x in v)) for a list or tuple v: HASHED with *h set when it
   could be hashed from its items */
static PyObject *cseq_tuple(FPObject *self, PyObject *v, PyObject *key, long d, Py_hash_t *h)
{
    Items its = {NULL, 0, 0};
    PyObject *res = NULL;
    for (Py_ssize_t i = 0; i < Py_SIZE(v); i++) {
        PyObject *x = PyList_CheckExact(v) ? PyList_GET_ITEM(v, i) : PyTuple_GET_ITEM(v, i);
        Py_INCREF(x);
        int e = push_fp(self, &its, x, d);
        Py_DECREF(x);
        if (e < 0) goto done;
    }
    if (hash_ok) {
        int ok = 1;
        Py_uhash_t acc = XXPRIME_5;
        for (Py_ssize_t i = 0; i < its.len && ok; i++) acc = xx_step(acc, (Py_uhash_t)item_hash(&its.v[i], &ok));
        if (ok) {
            *h = xx_pair(PyObject_Hash(key), xx_end(acc, its.len));
            res = HASHED;
            goto done;
        }
    }
    {
        PyObject *t = PyTuple_New(its.len);
        if (!t) goto done;
        for (Py_ssize_t i = 0; i < its.len; i++) {
            PyObject *x = item_value(&its.v[i]);
            if (!x) { Py_DECREF(t); goto done; }
            PyTuple_SET_ITEM(t, i, x);
        }
        res = PyTuple_Pack(2, key, t);
        Py_DECREF(t);
    }
done:
    items_clear(&its);
    return res;
}

static PyObject *a_co_code, *a_co_names, *a_co_varnames, *a_co_freevars, *a_co_argcount, *a_co_flags, *a_co_consts;

/* self.code_alias.get(h, h) */
static PyObject *alias_get(FPObject *self, PyObject *h)
{
    PyObject *r = PyDict_GetItemWithError(self->code_alias, h);
    if (r) return Py_NewRef(r);
    if (PyErr_Occurred()) return NULL;
    return Py_NewRef(h);
}

/* Fingerprinter.code */
static PyObject *ccode(FPObject *self, PyObject *c)
{
    PyObject *key = PyLong_FromVoidPtr(c);
    if (!key) return NULL;
    PyObject *h = PyDict_GetItemWithError(self->code_memo, key);
    if (h && h != Py_None) { Py_DECREF(key); return Py_NewRef(h); }
    if (!h && PyErr_Occurred()) { Py_DECREF(key); return NULL; }
    if (self->base != Py_None) {
        h = PyDict_GetItemWithError(self->base, key);
        if (h && h != Py_None) {
            PyObject *h2 = alias_get(self, h);
            if (!h2 || PyDict_SetItem(self->code_memo, key, h2) < 0) { Py_XDECREF(h2); Py_DECREF(key); return NULL; }
            Py_DECREF(key);
            return h2;
        }
        if (!h && PyErr_Occurred()) { Py_DECREF(key); return NULL; }
    }
    if (PyList_Append(self->code_objects, c) < 0) { Py_DECREF(key); return NULL; }
    PyObject *parts = PyList_New(0), *consts = NULL, *it = NULL, *k, *res = NULL;
    if (!parts) { Py_DECREF(key); return NULL; }
    PyObject *attrs[6] = {a_co_code, a_co_names, a_co_varnames, a_co_freevars, a_co_argcount, a_co_flags};
    for (int i = 0; i < 6; i++) {
        PyObject *x = PyObject_GetAttr(c, attrs[i]);
        if (!x || PyList_Append(parts, x) < 0) { Py_XDECREF(x); goto done; }
        Py_DECREF(x);
    }
    consts = PyObject_GetAttr(c, a_co_consts);
    if (!consts) goto done;
    it = PyObject_GetIter(consts);
    if (!it) goto done;
    while ((k = PyIter_Next(it)) != NULL) {
        PyObject *p;
        int r = PyObject_IsInstance(k, (PyObject *)&PyCode_Type);
        if (r < 0) { Py_DECREF(k); goto done; }
        if (r) {
            p = ccode(self, k);
        } else {
            PyObject *two_[2] = {PyObject_GetAttr((PyObject *)Py_TYPE(k), a_name), NULL};
            if (two_[0]) two_[1] = PyObject_Repr(k);
            p = pack_steal(2, two_);
        }
        Py_DECREF(k);
        if (!p || PyList_Append(parts, p) < 0) { Py_XDECREF(p); goto done; }
        Py_DECREF(p);
    }
    if (PyErr_Occurred()) goto done;
    {
        PyObject *tup = PyList_AsTuple(parts);
        if (!tup) goto done;
        Py_hash_t hh = PyObject_Hash(tup);
        Py_DECREF(tup);
        if (hh == -1) goto done;
        PyObject *hv = PyLong_FromSsize_t(hh);
        if (!hv) goto done;
        res = alias_get(self, hv);
        Py_DECREF(hv);
        if (res && PyDict_SetItem(self->code_memo, key, res) < 0) Py_CLEAR(res);
    }
done:
    Py_XDECREF(it); Py_XDECREF(consts); Py_DECREF(parts); Py_DECREF(key);
    return res;
}

static PyObject *FP_code(PyObject *op, PyObject *c)
{
    return ccode((FPObject *)op, c);
}

static PyObject *FP_memo(PyObject *op, PyObject *unused);

static PyObject *FP_seq(PyObject *op, PyObject *const *args, Py_ssize_t nargs);
static PyObject *FP_dict(PyObject *op, PyObject *const *args, Py_ssize_t nargs);

static PyMethodDef fp_methods[] = {
    {"code", FP_code, METH_O, "Fingerprinter.code"},
    {"seq", (PyCFunction)(void (*)(void))FP_seq, METH_FASTCALL, "Fingerprinter._seq(v, d)"},
    {"dict", (PyCFunction)(void (*)(void))FP_dict, METH_FASTCALL, "Fingerprinter._dict(v, depth)"},
    {"memo", FP_memo, METH_NOARGS, "{id: fingerprint} of the objects fingerprinted so far (busy ones: None)"},
    {NULL, NULL, 0, NULL},
};

/* _fp_function's _NONE_FP if d is None else self.fp(d, depth + 1), as an item */
static int push_def(FPObject *self, Items *it, PyObject *x, long d)
{
    if (x == Py_None) return items_push(it, Py_NewRef(Py_None), n_none);
    PyObject *r = fp(self, x, d);
    if (!r) return -1;
    return items_push(it, r, NULL);
}

static PyObject *cfunction(FPObject *self, PyObject *v, long depth, Py_hash_t *h)
{
    /* cells first (as the Python code computes them before the tuple), then qualname, code,
       defaults, kwdefaults; an empty cell (or a ValueError inside) is ("empty",) */
    Items cells = {NULL, 0, 0};
    Item fdef = {NULL, NULL}, fkw = {NULL, NULL};
    PyObject *qual = NULL, *codeh = NULL, *res = NULL;
    PyObject *closure = PyObject_GetAttr(v, a_closure);
    if (!closure) return NULL;
    int truth = PyObject_IsTrue(closure);
    if (truth < 0) { Py_DECREF(closure); return NULL; }
    if (truth) {
        PyObject *it = PyObject_GetIter(closure), *c;
        Py_DECREF(closure);
        if (!it) return NULL;
        while ((c = PyIter_Next(it)) != NULL) {
            PyObject *x = PyObject_GetAttr(c, a_cell_contents);
            Py_DECREF(c);
            int e;
            if (x) {
                /* _cell: self.fp(cell.cell_contents, depth + 1), a ValueError: ("empty",) */
                int err = 0;
                PyObject *n = prim_get(Py_TYPE(x), &err);
                if (n) {
                    e = items_push(&cells, x, n);
                    Py_DECREF(n);
                } else {
                    PyObject *r = err ? NULL : fp_np(self, x, depth + 1);
                    Py_DECREF(x);
                    if (!r) {
                        if (!PyErr_ExceptionMatches(PyExc_ValueError)) { Py_DECREF(it); goto done; }
                        PyErr_Clear();
                        r = Py_NewRef(empty_cell);
                    }
                    e = items_push(&cells, r, NULL);
                }
            } else {
                if (!PyErr_ExceptionMatches(PyExc_ValueError)) { Py_DECREF(it); goto done; }
                PyErr_Clear();
                e = items_push(&cells, Py_NewRef(empty_cell), NULL);
            }
            if (e < 0) { Py_DECREF(it); goto done; }
        }
        Py_DECREF(it);
        if (PyErr_Occurred()) goto done;
    } else {
        Py_DECREF(closure);
    }
    qual = PyObject_GetAttr(v, a_qualname);
    if (!qual) goto done;
    {
        PyObject *code = PyObject_GetAttr(v, a_code);
        if (!code) goto done;
        codeh = ccode(self, code);
        Py_DECREF(code);
        if (!codeh) goto done;
    }
    {
        Items one = {NULL, 0, 0};
        PyObject *x = PyObject_GetAttr(v, a_defaults);
        if (!x) goto done;
        int e = push_def(self, &one, x, depth + 1);
        Py_DECREF(x);
        if (e < 0) { items_clear(&one); goto done; }
        fdef = one.v[0];
        PyMem_Free(one.v);
        one = (Items){NULL, 0, 0};
        x = PyObject_GetAttr(v, a_kwdefaults);
        if (!x) goto done;
        e = push_def(self, &one, x, depth + 1);
        Py_DECREF(x);
        if (e < 0) { items_clear(&one); goto done; }
        fkw = one.v[0];
        PyMem_Free(one.v);
    }
    if (hash_ok && PyUnicode_CheckExact(qual) && PyLong_CheckExact(codeh)) {
        int ok = 1;
        Py_uhash_t cacc = XXPRIME_5;
        for (Py_ssize_t i = 0; i < cells.len && ok; i++) {
            if (cells.v[i].n == NULL && cells.v[i].obj == empty_cell) cacc = xx_step(cacc, (Py_uhash_t)empty_hash);
            else cacc = xx_step(cacc, (Py_uhash_t)item_hash(&cells.v[i], &ok));
        }
        Py_hash_t hd = item_hash(&fdef, &ok), hk = item_hash(&fkw, &ok);
        if (ok) {
            Py_uhash_t acc = xx_step(XXPRIME_5, (Py_uhash_t)PyObject_Hash(k_F));
            acc = xx_step(acc, (Py_uhash_t)PyObject_Hash(qual));
            acc = xx_step(acc, (Py_uhash_t)PyObject_Hash(codeh));
            acc = xx_step(acc, (Py_uhash_t)hd);
            acc = xx_step(acc, (Py_uhash_t)hk);
            acc = xx_step(acc, (Py_uhash_t)xx_end(cacc, cells.len));
            *h = xx_end(acc, 6);
            res = HASHED;
            goto done;
        }
    }
    {
        PyObject *ct = PyTuple_New(cells.len);
        if (!ct) goto done;
        for (Py_ssize_t i = 0; i < cells.len; i++) {
            PyObject *x = item_value(&cells.v[i]);
            if (!x) { Py_DECREF(ct); goto done; }
            PyTuple_SET_ITEM(ct, i, x);
        }
        PyObject *it[6] = {Py_NewRef(k_F), Py_NewRef(qual), Py_NewRef(codeh), item_value(&fdef), item_value(&fkw), ct};
        res = pack_steal(6, it);
    }
done:
    items_clear(&cells);
    Py_XDECREF(fdef.obj); Py_XDECREF(fdef.n); Py_XDECREF(fkw.obj); Py_XDECREF(fkw.n);
    Py_XDECREF(qual); Py_XDECREF(codeh);
    return res;
}

/* 1 when marshal.dumps(x, 2) takes x as a leaf that it writes without fail: exact int, float,
   complex, bytes, str, bool, None */
static inline int marshal_atom(PyObject *x)
{
    PyTypeObject *t = Py_TYPE(x);
    return t == &PyUnicode_Type || t == &PyLong_Type || t == &PyFloat_Type || t == &PyBool_Type
        || t == &PyBytes_Type || t == &PyComplex_Type || x == Py_None;
}

/* 1 when marshal's w_object rejects x itself ("unmarshallable object", a ValueError): none of
   the singletons and types it writes, no code object, no buffer */
static inline int marshal_reject(PyObject *x)
{
    PyTypeObject *t = Py_TYPE(x);
    if (x == Py_None || x == PyExc_StopIteration || x == Py_Ellipsis || x == Py_False || x == Py_True) return 0;
    if (t == &PyLong_Type || t == &PyFloat_Type || t == &PyComplex_Type || t == &PyBytes_Type || t == &PyUnicode_Type
        || t == &PyTuple_Type || t == &PyList_Type || t == &PyDict_Type || t == &PySet_Type || t == &PyFrozenSet_Type
        || t == &PyCode_Type)
        return 0;
    return !PyObject_CheckBuffer(x);
}

/* 1 when marshal.dumps(v, 2) of this container is known to raise ValueError: in marshal's order
   (dict: key, value, key, value...; list, tuple, set: the items), the items before the first one
   that is not a leaf are leaves it writes without fail, and that one it rejects. 0: not known
   (marshal runs; always for a set). */
static int marshal_fails(PyObject *v, PyTypeObject *t)
{
    if (t == &PyDict_Type) {
        Py_ssize_t pos = 0;
        PyObject *k, *x;
        while (PyDict_Next(v, &pos, &k, &x)) {
            if (!marshal_atom(k)) return marshal_reject(k);
            if (!marshal_atom(x)) return marshal_reject(x);
        }
        return 0;
    }
    if (t == &PyList_Type || t == &PyTuple_Type) {
        Py_ssize_t n = Py_SIZE(v);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *x = t == &PyList_Type ? PyList_GET_ITEM(v, i) : PyTuple_GET_ITEM(v, i);
            if (!marshal_atom(x)) return marshal_reject(x);
        }
        return 0;
    }
    return 0;  /* a set: marshal runs */
}

static PyObject *fp_inner(FPObject *self, PyObject *v, PyTypeObject *t, long depth, Py_hash_t *h)
{
    PyObject *tname = NULL;
    if (t == &PyDict_Type) tname = t_dict;
    else if (t == &PyList_Type) tname = t_list;
    else if (t == &PyTuple_Type) tname = t_tuple;
    else if (t == &PySet_Type) tname = t_set;
    else if (t == &PyFrozenSet_Type) tname = t_frozenset;
    if (tname) {
        int fails = marshal_fails(v, t);
        /* the audit event of the marshal.dumps call that is skipped, as marshal raises it */
        if (fails && PySys_Audit("marshal.dumps", "Oi", v, 2) < 0) return NULL;
        if (!fails) {
            PyObject *args[2] = {v, two};
            PyObject *b = PyObject_Vectorcall(marshal_dumps, args, 2, NULL);
            if (b) {
                if (hash_ok && PyBytes_CheckExact(b)) {
                    Py_uhash_t acc = xx_step(XXPRIME_5, (Py_uhash_t)PyObject_Hash(k_m));
                    acc = xx_step(acc, (Py_uhash_t)PyObject_Hash(tname));
                    acc = xx_step(acc, (Py_uhash_t)PyObject_Hash(b));
                    Py_DECREF(b);
                    *h = xx_end(acc, 3);
                    return HASHED;
                }
                PyObject *r = PyTuple_Pack(3, k_m, tname, b);
                Py_DECREF(b);
                return r;
            }
            if (!PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;
            PyErr_Clear();
        }
        if (t == &PyDict_Type) return cdict(self, v, depth, h);
        if (t == &PyTuple_Type && PyTuple_GET_SIZE(v) > 0) {
            int all = 1;
            for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(v); i++) {
                int r = PyObject_IsInstance(PyTuple_GET_ITEM(v, i), (PyObject *)&PyType_Type);
                if (r < 0) return NULL;
                if (!r) { all = 0; break; }
            }
            if (all) {
                PyObject *l = seq(self, v, depth + 1);
                if (!l) return NULL;
                PyObject *fs = PyFrozenSet_New(l);
                Py_DECREF(l);
                if (!fs) return NULL;
                PyObject *r = PyTuple_Pack(2, k_tc, fs);
                Py_DECREF(fs);
                return r;
            }
        }
        if (t == &PyList_Type || t == &PyTuple_Type) return cseq_tuple(self, v, tname, depth + 1, h);
        PyObject *l = seq(self, v, depth + 1);
        if (!l) return NULL;
        PyObject *c, *key;
        c = PyFrozenSet_New(l);
        key = k_s;
        Py_DECREF(l);
        if (!c) return NULL;
        PyObject *r = PyTuple_Pack(2, key, c);
        Py_DECREF(c);
        return r;
    }
    int r = PyObject_IsInstance(v, enum_class);
    if (r < 0) return NULL;
    if (r) {
        PyObject *it[5] = {Py_NewRef(k_E), NULL, NULL, NULL, NULL};
        if ((it[1] = PyObject_GetAttr((PyObject *)t, a_module)) && (it[2] = PyObject_GetAttr((PyObject *)t, a_qualname))
            && (it[3] = PyObject_GetAttr(v, a_ename)))
            it[4] = fp_attr(self, v, a_value, depth + 1);
        return pack_steal(5, it);
    }
    r = PyObject_IsInstance(v, (PyObject *)&PyType_Type);
    if (r < 0) return NULL;
    if (r) {
        PyObject *it[3] = {Py_NewRef(k_C), NULL, NULL};
        if ((it[1] = PyObject_GetAttr(v, a_module))) it[2] = PyObject_GetAttr(v, a_qualname);
        return pack_steal(3, it);
    }
    if (t == &PyModule_Type) {
        PyObject *it[2] = {Py_NewRef(k_M), PyObject_GetAttr(v, a_name)};
        return pack_steal(2, it);
    }
    if (t == &PyFunction_Type) return cfunction(self, v, depth, h);
    if (t == &PyStaticMethod_Type || t == &PyClassMethod_Type) {
        PyObject *it[2] = {Py_NewRef(t == &PyStaticMethod_Type ? t_staticmethod : t_classmethod),
                           fp_attr(self, v, a_func, depth + 1)};
        return pack_steal(2, it);
    }
    if (t == &PyProperty_Type) {
        PyObject *it[4] = {Py_NewRef(k_prop), NULL, NULL, NULL};
        if ((it[1] = fp_attr(self, v, a_fget, depth + 1)) && (it[2] = fp_attr(self, v, a_fset, depth + 1)))
            it[3] = fp_attr(self, v, a_fdel, depth + 1);
        return pack_steal(4, it);
    }
    if (t == &PyMethod_Type) {
        PyObject *it[3] = {Py_NewRef(k_meth), NULL, NULL};
        if ((it[1] = fp_attr(self, v, a_func, depth + 1))) it[2] = fp_attr(self, v, a_self, depth + 1);
        return pack_steal(3, it);
    }
    PyObject *owner = get_owner(self);
    if (!owner) return NULL;
    PyObject *dv = PyLong_FromLong(depth);
    if (!dv) { Py_DECREF(owner); return NULL; }
    PyObject *args[4] = {owner, v, (PyObject *)t, dv};
    PyObject *res = PyObject_VectorcallMethod(m_rest, args, 4 | PY_VECTORCALL_ARGUMENTS_OFFSET, NULL);
    Py_DECREF(dv);
    Py_DECREF(owner);
    return res;
}

/* memo[key] = val (a value that an fp nested in hash or repr stored first is replaced, as the
   dict assignment replaces it); val: a new reference, which the table takes */
static int memo_set(FPObject *self, uintptr_t key, PyObject *val)
{
    struct slot *s = tab_find(self, key);
    if (s) {
        if (s->val != BUSY) Py_DECREF(s->val);
        s->val = val;
        return 0;
    }
    return tab_put(self, key, val);
}

/* owner.<name>(v, t, depth) */
static PyObject *call_py(FPObject *self, PyObject *name, PyObject *v, PyTypeObject *t, long depth)
{
    PyObject *owner = get_owner(self);
    if (!owner) return NULL;
    PyObject *dv = PyLong_FromLong(depth);
    if (!dv) { Py_DECREF(owner); return NULL; }
    PyObject *args[4] = {owner, v, (PyObject *)t, dv};
    PyObject *res = PyObject_VectorcallMethod(name, args, 4 | PY_VECTORCALL_ARGUMENTS_OFFSET, NULL);
    Py_DECREF(dv);
    Py_DECREF(owner);
    return res;
}

/* Fingerprinter.fp after its _PRIM_NAME lookup (which found nothing); t = type(v), held */
static PyObject *fp_obj(FPObject *self, PyObject *v, PyTypeObject *t, long depth)
{
    uintptr_t key = (uintptr_t)v;
    struct slot *s = tab_find(self, key);
    if (s != NULL && s->val != BUSY) return Py_NewRef(s->val);
    if (s != NULL || depth > 12) {
        PyObject *it[2] = {Py_NewRef(k_cycle), PyObject_GetAttr((PyObject *)t, a_name)};
        return pack_steal(2, it);
    }
    PyObject *then = NULL;
    if (!plain_meta(t)) {
        /* a metaclass with its own __eq__ / __hash__: fp's tests of t in Python (_fp_head, then
           _fp_plain or _fp), with v in busy around the second step as in fp */
        PyObject *owner = get_owner(self);
        if (!owner) return NULL;
        PyObject *args[3] = {owner, v, (PyObject *)t};
        PyObject *r = PyObject_VectorcallMethod(m_head, args, 3 | PY_VECTORCALL_ARGUMENTS_OFFSET, NULL);
        Py_DECREF(owner);
        if (!r) return NULL;
        if (r == Py_None) then = m_fp;
        else if (r == Py_False) then = m_fp_plain;
        else {
            /* the marshal hash: memo[key] = out; return out */
            if (memo_set(self, key, Py_NewRef(r)) < 0) { Py_DECREF(r); return NULL; }
            return r;
        }
        Py_DECREF(r);
    }
    if (tab_put(self, key, BUSY) < 0) return NULL;
    Py_hash_t h = -1;
    PyObject *out = then ? call_py(self, then, v, t, depth) : fp_inner(self, v, t, depth, &h);
    tab_drop(self, key);  /* busy.discard(key) */
    if (!out) return NULL;
    if (out == HASHED) out = NULL;  /* (h: the hash of the tuple, which hashes without fail) */
    else h = PyObject_Hash(out);
    if (h == -1) {
        if (!PyErr_ExceptionMatches(PyExc_TypeError)) { Py_DECREF(out); return NULL; }
        PyErr_Clear();
        PyObject *text = PyObject_Repr(out);
        if (!text) { Py_DECREF(out); return NULL; }
        h = PyObject_Hash(text);
        Py_DECREF(text);
        if (h == -1) { Py_DECREF(out); return NULL; }
    }
    Py_XDECREF(out);
    PyObject *hv = PyLong_FromSsize_t(h);
    if (!hv) return NULL;
    if (memo_set(self, key, Py_NewRef(hv)) < 0) {
        Py_DECREF(hv);
        return NULL;
    }
    return hv;
}

/* fp without its _PRIM_NAME lookup (the caller made it and found nothing) */
static PyObject *fp_np(FPObject *self, PyObject *v, long depth)
{
    PyTypeObject *t = Py_TYPE(v);
    Py_INCREF(t);
    PyObject *r = fp_obj(self, v, t, depth);
    Py_DECREF(t);
    return r;
}

/* Fingerprinter.fp */
static PyObject *fp(FPObject *self, PyObject *v, long depth)
{
    PyTypeObject *t = Py_TYPE(v);
    if (plain_meta(t)) {
        PyObject *n = prim_name(t);
        if (n) return PyTuple_Pack(2, n, v);
        Py_INCREF(t);
        PyObject *r = fp_obj(self, v, t, depth);
        Py_DECREF(t);
        return r;
    }
    Py_INCREF(t);
    int err = 0;
    PyObject *n = prim_get(t, &err), *r;
    if (n) {
        r = PyTuple_Pack(2, n, v);
        Py_DECREF(n);
    } else {
        r = err ? NULL : fp_obj(self, v, t, depth);
    }
    Py_DECREF(t);
    return r;
}

static int depth_arg(PyObject *dv, long *depth)
{
    if (!PyLong_Check(dv)) { PyErr_SetString(PyExc_TypeError, "depth: an int"); return -1; }
    int overflow = 0;
    *depth = PyLong_AsLongAndOverflow(dv, &overflow);
    if (overflow) *depth = overflow > 0 ? 1L << 40 : -(1L << 40);
    if (*depth == -1 && PyErr_Occurred()) return -1;
    return 0;
}

/* Fingerprinter._seq and _dict (instance attributes in C mode, so that the Python branches that
   call them, _fp_plain and _fp, use the memo of this walk as the Python ones use self.memo) */
/* the Python method of the class (a depth that is not an int: as FP_call does) */
static PyObject *py_method(FPObject *self, const char *name, PyObject *v, PyObject *dv)
{
    PyObject *owner = get_owner(self);
    if (!owner) return NULL;
    PyObject *meth = PyObject_GetAttrString((PyObject *)Py_TYPE(owner), name);
    PyObject *r = meth ? PyObject_CallFunctionObjArgs(meth, owner, v, dv, NULL) : NULL;
    Py_XDECREF(meth);
    Py_DECREF(owner);
    return r;
}

static PyObject *FP_seq(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    long d;
    if (nargs != 2) { PyErr_SetString(PyExc_TypeError, "_seq(v, d)"); return NULL; }
    if (!PyLong_Check(args[1])) return py_method((FPObject *)op, "_seq", args[0], args[1]);
    if (depth_arg(args[1], &d) < 0) return NULL;
    return seq((FPObject *)op, args[0], d);
}

static PyObject *FP_dict(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    long d;
    if (nargs != 2) { PyErr_SetString(PyExc_TypeError, "_dict(v, depth)"); return NULL; }
    if (!PyLong_Check(args[1])) return py_method((FPObject *)op, "_dict", args[0], args[1]);
    if (depth_arg(args[1], &d) < 0) return NULL;
    return cdict((FPObject *)op, args[0], d, NULL);
}

static PyObject *FP_memo(PyObject *op, PyObject *unused)
{
    FPObject *self = (FPObject *)op;
    PyObject *d = PyDict_New();
    if (!d) return NULL;
    for (size_t j = 0; j <= self->mask; j++) {
        struct slot *s = &self->tab[j];
        if (s->key == 0 || s->val == NULL) continue;
        PyObject *k = PyLong_FromVoidPtr((void *)s->key);
        if (!k || PyDict_SetItem(d, k, s->val == BUSY ? Py_None : s->val) < 0) { Py_XDECREF(k); Py_DECREF(d); return NULL; }
        Py_DECREF(k);
    }
    return d;
}

static PyObject *FP_call(PyObject *op, PyObject *const *args, size_t nargsf, PyObject *kwnames)
{
    FPObject *self = (FPObject *)op;
    Py_ssize_t nargs = PyVectorcall_NARGS(nargsf);
    PyObject *dv = NULL;
    Py_ssize_t nkw = kwnames ? PyTuple_GET_SIZE(kwnames) : 0;
    if (nargs + nkw < 1 || nargs + nkw > 2 || nargs > 2) {
        PyErr_SetString(PyExc_TypeError, "fp(v, depth=0)");
        return NULL;
    }
    if (nkw) {
        for (Py_ssize_t i = 0; i < nkw; i++) {
            PyObject *name = PyTuple_GET_ITEM(kwnames, i);
            int is_depth = PyUnicode_Check(name) && PyUnicode_CompareWithASCIIString(name, "depth") == 0;
            int is_v = PyUnicode_Check(name) && PyUnicode_CompareWithASCIIString(name, "v") == 0;
            if (is_depth && nargs + i >= 1 && dv == NULL) dv = args[nargs + i];
            else if (is_v && nargs == 0 && i == 0) continue;
            else { PyErr_SetString(PyExc_TypeError, "fp(v, depth=0)"); return NULL; }
        }
    }
    PyObject *v = args[0];
    if (nargs == 2) dv = args[1];
    long depth = 0;
    if (dv) {
        if (!PyLong_Check(dv)) {
            /* not an int: the Python method (its nested calls come back here) */
            PyObject *owner = get_owner(self);
            if (!owner) return NULL;
            PyObject *meth = PyObject_GetAttrString((PyObject *)Py_TYPE(owner), "fp");
            PyObject *r = meth ? PyObject_CallFunctionObjArgs(meth, owner, v, dv, NULL) : NULL;
            Py_XDECREF(meth);
            Py_DECREF(owner);
            return r;
        }
        int overflow = 0;
        depth = PyLong_AsLongAndOverflow(dv, &overflow);
        if (overflow) { if (overflow > 0) depth = 1L << 40; else depth = -(1L << 40); }
        if (depth == -1 && PyErr_Occurred()) return NULL;
    }
    return fp(self, v, depth);
}

static vectorcallfunc fp_vectorcall_ptr = FP_call;

static PyObject *FP_new(PyTypeObject *type, PyObject *args, PyObject *kw)
{
    PyObject *owner, *memo, *busy, *code_memo, *base, *code_alias, *code_objects;
    if (!PyArg_ParseTuple(args, "OO!O!O!OO!O!", &owner, &PyDict_Type, &memo, &PySet_Type, &busy, &PyDict_Type, &code_memo,
                          &base, &PyDict_Type, &code_alias, &PyList_Type, &code_objects))
        return NULL;
    if (base != Py_None && !PyDict_CheckExact(base)) { PyErr_SetString(PyExc_TypeError, "base: a dict or None"); return NULL; }
    if (!PyDict_CheckExact(memo) || !PyDict_CheckExact(code_memo) || !PyDict_CheckExact(code_alias) || !PySet_CheckExact(busy)
        || !PyList_CheckExact(code_objects)) { PyErr_SetString(PyExc_TypeError, "plain containers"); return NULL; }
    FPObject *self = (FPObject *)type->tp_alloc(type, 0);
    if (!self) return NULL;
    self->owner = PyWeakref_NewRef(owner, NULL);
    if (!self->owner) { Py_DECREF(self); return NULL; }
    self->memo = Py_NewRef(memo);
    self->busy = Py_NewRef(busy);
    self->code_memo = Py_NewRef(code_memo);
    self->base = Py_NewRef(base);
    self->code_alias = Py_NewRef(code_alias);
    self->code_objects = Py_NewRef(code_objects);
    self->tab = PyMem_Calloc(1024, sizeof(struct slot));
    if (!self->tab) { Py_DECREF(self); return PyErr_NoMemory(); }
    self->mask = 1023;
    self->used = 0;
    return (PyObject *)self;
}

static PyObject *FP_tpcall(PyObject *op, PyObject *args, PyObject *kw)
{
    return PyVectorcall_Call(op, args, kw);
}

static void FP_dealloc(PyObject *op)
{
    FPObject *self = (FPObject *)op;
    Py_XDECREF(self->owner);
    Py_XDECREF(self->memo);
    Py_XDECREF(self->busy);
    Py_XDECREF(self->code_memo);
    Py_XDECREF(self->base);
    Py_XDECREF(self->code_alias);
    Py_XDECREF(self->code_objects);
    if (self->tab) {
        for (size_t j = 0; j <= self->mask; j++) {
            PyObject *x = self->tab[j].val;
            if (x != NULL && x != BUSY) Py_DECREF(x);
        }
        PyMem_Free(self->tab);
    }
    Py_TYPE(op)->tp_free(op);
}

static PyObject *FP_vc_new(PyTypeObject *type, PyObject *args, PyObject *kw);

typedef struct { FPObject base; vectorcallfunc vc; } FPVC;

static PyTypeObject FPType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_statefpc.FP",
    .tp_basicsize = sizeof(FPVC),
    .tp_dealloc = FP_dealloc,
    .tp_call = FP_tpcall,
    .tp_vectorcall_offset = offsetof(FPVC, vc),
    .tp_flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_VECTORCALL,
    .tp_new = FP_vc_new,
    .tp_methods = fp_methods,
};

static PyObject *FP_vc_new(PyTypeObject *type, PyObject *args, PyObject *kw)
{
    PyObject *o = FP_new(type, args, kw);
    if (o) ((FPVC *)o)->vc = fp_vectorcall_ptr;
    return o;
}


/* ---- the snapshot driver (statefp.snapshot's add_value, add_class and module loop) ---- */

typedef struct {
    PyObject_HEAD
    FPObject *fp;
    PyObject *f, *out, *seen, *fkeys, *objects, *skip, *py_add_value;
} AdderObject;

static PyObject *a_dict, *a_bases, *m_state, *m_startswith, *s_empty, *s_dot, *k_O, *k_A, *k_G, *s_sqlglot,
    *func_types = NULL, *type_enum = NULL;

static int set_out(AdderObject *self, PyObject *key, PyObject *v)
{
    PyObject *r = fp(self->fp, v, 0);
    if (!r) return -1;
    int e = PyObject_SetItem(self->out, key, r);
    Py_DECREF(r);
    return e;
}

/* mod.startswith("sqlglot") for any object, as Python evaluates it */
static int starts_sqlglot(PyObject *mod)
{
    if (PyUnicode_CheckExact(mod)) {
        Py_ssize_t r = PyUnicode_Tailmatch(mod, s_sqlglot, 0, PY_SSIZE_T_MAX, -1);
        return (int)r;
    }
    PyObject *r = PyObject_CallMethodOneArg(mod, m_startswith, s_sqlglot);
    if (!r) return -1;
    int t = PyObject_IsTrue(r);
    Py_DECREF(r);
    return t;
}

static int add_value(AdderObject *self, PyObject *key, PyObject *v)
{
    PyTypeObject *t = Py_TYPE(v);
    if (!plain_meta(t)) {
        /* a metaclass with its own __eq__ / __hash__: snapshot's Python add_value, whose tests
           of t (prim(t), t in function_types, plain, tables) run them as Python does */
        PyObject *args[2] = {key, v};
        PyObject *r = PyObject_Vectorcall(self->py_add_value, args, 2, NULL);
        if (!r) return -1;
        Py_DECREF(r);
        return 0;
    }
    int r = PySequence_Contains(func_types, (PyObject *)t);
    if (r < 0) return -1;
    if (r && PySet_Add(self->fkeys, key) < 0) return -1;
    PyObject *mod = NULL;
    if (PyObject_GetOptionalAttr((PyObject *)t, a_module, &mod) < 0) return -1;
    if (mod == NULL) mod = Py_NewRef(s_empty);
    r = PyObject_IsTrue(mod);
    if (r < 0) { Py_DECREF(mod); return -1; }
    if (!r) Py_SETREF(mod, Py_NewRef(s_empty));
    r = starts_sqlglot(mod);
    Py_DECREF(mod);
    if (r < 0) return -1;
    if (r) {
        r = PyObject_IsInstance(v, type_enum);
        if (r < 0) return -1;
        if (!r && t != &PyFunction_Type) {
            PyObject *args[3] = {self->f, v, (PyObject *)t};
            PyObject *state = PyObject_VectorcallMethod(m_state, args, 3 | PY_VECTORCALL_ARGUMENTS_OFFSET, NULL);
            if (!state) return -1;
            r = PyObject_IsTrue(state);
            if (r < 0) { Py_DECREF(state); return -1; }
            if (r) {
                if (!PyTuple_Check(key)) {
                    Py_DECREF(state);
                    PyErr_SetString(PyExc_TypeError, "key: a tuple");
                    return -1;
                }
                PyObject *it = PyObject_GetIter(state), *item;
                Py_DECREF(state);
                if (!it) return -1;
                Py_ssize_t n = PyTuple_GET_SIZE(key);
                while ((item = PyIter_Next(it)) != NULL) {
                    PyObject *pair = PySequence_Tuple(item);
                    Py_DECREF(item);
                    if (!pair) { Py_DECREF(it); return -1; }
                    if (PyTuple_GET_SIZE(pair) != 2) {
                        Py_DECREF(pair); Py_DECREF(it);
                        PyErr_SetString(PyExc_ValueError, "state: not a pair");
                        return -1;
                    }
                    PyObject *k2 = PyTuple_New(n < 1 ? 2 : n + 1);
                    if (!k2) { Py_DECREF(pair); Py_DECREF(it); return -1; }
                    PyTuple_SET_ITEM(k2, 0, Py_NewRef(k_O));
                    Py_ssize_t j = 1;
                    for (Py_ssize_t i = 1; i < n; i++) PyTuple_SET_ITEM(k2, j++, Py_NewRef(PyTuple_GET_ITEM(key, i)));
                    PyTuple_SET_ITEM(k2, j, Py_NewRef(PyTuple_GET_ITEM(pair, 0)));
                    int e = set_out(self, k2, PyTuple_GET_ITEM(pair, 1));
                    Py_DECREF(k2);
                    Py_DECREF(pair);
                    if (e < 0) { Py_DECREF(it); return -1; }
                }
                Py_DECREF(it);
                return PyErr_Occurred() ? -1 : 0;
            }
            Py_DECREF(state);
        }
    }
    if ((t == &PyDict_Type || t == &PySet_Type || t == &PyFrozenSet_Type) && PyObject_SetItem(self->objects, key, v) < 0)
        return -1;
    return set_out(self, key, v);
}

static PyObject *items_list(PyObject *mapping)
{
    PyObject *items = PyObject_CallMethodNoArgs(mapping, m_items);
    if (!items) return NULL;
    PyObject *l = PySequence_List(items);
    Py_DECREF(items);
    return l;
}

/* v.__module__ == cls.__module__ and v.__qualname__.startswith(cls.__qualname__ + ".") */
static int nested_class(PyObject *v, PyObject *cls)
{
    PyObject *m1 = PyObject_GetAttr(v, a_module);
    if (!m1) return -1;
    PyObject *m2 = PyObject_GetAttr(cls, a_module);
    if (!m2) { Py_DECREF(m1); return -1; }
    PyObject *eq = PyObject_RichCompare(m1, m2, Py_EQ);
    Py_DECREF(m1); Py_DECREF(m2);
    if (!eq) return -1;
    int r = PyObject_IsTrue(eq);
    Py_DECREF(eq);
    if (r <= 0) return r;
    PyObject *q = PyObject_GetAttr(v, a_qualname);
    if (!q) return -1;
    PyObject *meth = PyObject_GetAttr(q, m_startswith);
    Py_DECREF(q);
    if (!meth) return -1;
    PyObject *cq = PyObject_GetAttr(cls, a_qualname);
    if (!cq) { Py_DECREF(meth); return -1; }
    PyObject *pre = PyNumber_Add(cq, s_dot);
    Py_DECREF(cq);
    if (!pre) { Py_DECREF(meth); return -1; }
    PyObject *res = PyObject_CallOneArg(meth, pre);
    Py_DECREF(meth); Py_DECREF(pre);
    if (!res) return -1;
    r = PyObject_IsTrue(res);
    Py_DECREF(res);
    return r;
}

static int add_class(AdderObject *self, PyObject *cls, PyObject *mod, PyObject *path)
{
    int r = PySet_Contains(self->seen, cls);
    if (r) return r < 0 ? -1 : 0;
    if (PySet_Add(self->seen, cls) < 0) return -1;
    int ret = -1;
    PyObject *items = NULL, *bases = NULL, *key = NULL;
    if (mod && mod != Py_None) {
        r = PyObject_IsTrue(mod);
        if (r < 0) return -1;
    } else r = 0;
    mod = r ? Py_NewRef(mod) : PyObject_GetAttr(cls, a_module);
    if (!mod) return -1;
    if (path && path != Py_None) {
        r = PyObject_IsTrue(path);
        if (r < 0) { Py_DECREF(mod); return -1; }
    } else r = 0;
    path = r ? Py_NewRef(path) : PyObject_GetAttr(cls, a_qualname);
    if (!path) { Py_DECREF(mod); return -1; }
    PyObject *d = PyObject_GetAttr(cls, a_dict);
    if (!d) goto done;
    items = items_list(d);
    Py_DECREF(d);
    if (!items) goto done;
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(items); i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) { PyErr_SetString(PyExc_ValueError, "items"); goto done; }
        PyObject *name = PyTuple_GET_ITEM(pair, 0), *v = PyTuple_GET_ITEM(pair, 1);
        r = PySequence_Contains(self->skip, name);
        if (r < 0) goto done;
        if (r) continue;
        r = PyObject_IsInstance(v, (PyObject *)&PyType_Type);
        if (r < 0) goto done;
        if (r) {
            r = nested_class(v, cls);
            if (r < 0) goto done;
            if (r) {
                if (add_class(self, v, NULL, NULL) < 0) goto done;
                continue;
            }
        }
        key = PyTuple_Pack(4, k_A, mod, path, name);
        if (!key || add_value(self, key, v) < 0) goto done;
        Py_CLEAR(key);
    }
    bases = PyUnicode_InternFromString("__bases__");
    if (!bases) goto done;
    key = PyTuple_Pack(4, k_A, mod, path, bases);
    if (!key) goto done;
    {
        PyObject *b = PyObject_GetAttr(cls, a_bases);
        if (!b) goto done;
        r = set_out(self, key, b);
        Py_DECREF(b);
        if (r < 0) goto done;
    }
    ret = 0;
done:
    Py_XDECREF(key); Py_XDECREF(bases); Py_XDECREF(items); Py_DECREF(mod); Py_DECREF(path);
    return ret;
}

static int add_module(AdderObject *self, PyObject *mod_name, PyObject *m)
{
    PyObject *d = PyObject_GetAttr(m, a_dict);
    if (!d) return -1;
    PyObject *items = items_list(d);
    Py_DECREF(d);
    if (!items) return -1;
    int ret = -1, r;
    PyObject *key = NULL;
    for (Py_ssize_t i = 0; i < PyList_GET_SIZE(items); i++) {
        PyObject *pair = PyList_GET_ITEM(items, i);
        if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) { PyErr_SetString(PyExc_ValueError, "items"); goto done; }
        PyObject *name = PyTuple_GET_ITEM(pair, 0), *v = PyTuple_GET_ITEM(pair, 1);
        r = PySequence_Contains(self->skip, name);
        if (r < 0) goto done;
        if (r) continue;
        r = PyObject_IsInstance(v, (PyObject *)&PyType_Type);
        if (r < 0) goto done;
        if (r) {
            PyObject *vm = PyObject_GetAttr(v, a_module);
            if (!vm) goto done;
            PyObject *eq = PyObject_RichCompare(vm, mod_name, Py_EQ);
            Py_DECREF(vm);
            if (!eq) goto done;
            r = PyObject_IsTrue(eq);
            Py_DECREF(eq);
            if (r < 0) goto done;
            if (r) {
                if (add_class(self, v, NULL, NULL) < 0) goto done;
            } else {
                key = PyTuple_Pack(3, k_G, mod_name, name);
                if (!key || set_out(self, key, v) < 0) goto done;
                Py_CLEAR(key);
            }
            continue;
        }
        key = PyTuple_Pack(3, k_G, mod_name, name);
        if (!key) goto done;
        r = PyObject_IsInstance(v, (PyObject *)&PyModule_Type);
        if (r < 0) goto done;
        if (r) {
            PyObject *it[2] = {Py_NewRef(k_M), PyObject_GetAttr(v, a_name)};
            PyObject *val = pack_steal(2, it);
            if (!val) goto done;
            r = PyObject_SetItem(self->out, key, val);
            Py_DECREF(val);
            if (r < 0) goto done;
        } else if (add_value(self, key, v) < 0) goto done;
        Py_CLEAR(key);
    }
    ret = 0;
done:
    Py_XDECREF(key);
    Py_DECREF(items);
    return ret;
}

static PyObject *Adder_add_value(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) { PyErr_SetString(PyExc_TypeError, "add_value(key, v)"); return NULL; }
    if (add_value((AdderObject *)op, args[0], args[1]) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *Adder_add_class(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs < 1 || nargs > 3) { PyErr_SetString(PyExc_TypeError, "add_class(cls, mod=None, path=None)"); return NULL; }
    if (add_class((AdderObject *)op, args[0], nargs > 1 ? args[1] : NULL, nargs > 2 ? args[2] : NULL) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *Adder_add_module(PyObject *op, PyObject *const *args, Py_ssize_t nargs)
{
    if (nargs != 2) { PyErr_SetString(PyExc_TypeError, "add_module(mod_name, m)"); return NULL; }
    if (add_module((AdderObject *)op, args[0], args[1]) < 0) return NULL;
    Py_RETURN_NONE;
}

static PyMethodDef adder_methods[] = {
    {"add_value", (PyCFunction)(void (*)(void))Adder_add_value, METH_FASTCALL, NULL},
    {"add_class", (PyCFunction)(void (*)(void))Adder_add_class, METH_FASTCALL, NULL},
    {"add_module", (PyCFunction)(void (*)(void))Adder_add_module, METH_FASTCALL, NULL},
    {NULL, NULL, 0, NULL},
};

static PyTypeObject FPType;

static PyObject *Adder_new(PyTypeObject *type, PyObject *args, PyObject *kw)
{
    PyObject *f, *out, *seen, *fkeys, *objects, *skip, *pyadd, *fpo;
    if (!PyArg_ParseTuple(args, "OO!O!O!O!O!O", &f, &PyDict_Type, &out, &PySet_Type, &seen, &PySet_Type, &fkeys,
                          &PyDict_Type, &objects, &PyFrozenSet_Type, &skip, &pyadd))
        return NULL;
    if (!PyCallable_Check(pyadd)) { PyErr_SetString(PyExc_TypeError, "add_value: not callable"); return NULL; }
    fpo = PyObject_GetAttrString(f, "fp");
    if (!fpo) return NULL;
    if (Py_TYPE(fpo) != &FPType) { Py_DECREF(fpo); PyErr_SetString(PyExc_TypeError, "f.fp is not the C fp"); return NULL; }
    AdderObject *self = (AdderObject *)type->tp_alloc(type, 0);
    if (!self) { Py_DECREF(fpo); return NULL; }
    self->fp = (FPObject *)fpo;
    self->f = Py_NewRef(f); self->out = Py_NewRef(out); self->seen = Py_NewRef(seen);
    self->fkeys = Py_NewRef(fkeys); self->objects = Py_NewRef(objects); self->skip = Py_NewRef(skip);
    self->py_add_value = Py_NewRef(pyadd);
    return (PyObject *)self;
}

static void Adder_dealloc(PyObject *op)
{
    AdderObject *self = (AdderObject *)op;
    Py_XDECREF(self->fp); Py_XDECREF(self->f); Py_XDECREF(self->out); Py_XDECREF(self->seen);
    Py_XDECREF(self->fkeys); Py_XDECREF(self->objects); Py_XDECREF(self->skip); Py_XDECREF(self->py_add_value);
    Py_TYPE(op)->tp_free(op);
}

static PyTypeObject AdderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_statefpc.Adder",
    .tp_basicsize = sizeof(AdderObject),
    .tp_dealloc = Adder_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_methods = adder_methods,
    .tp_new = Adder_new,
};

static int adder_init(void)
{
#define S(var, text) if (!(var = PyUnicode_InternFromString(text))) return -1;
    S(a_dict, "__dict__") S(a_bases, "__bases__") S(m_state, "_state") S(m_startswith, "startswith") S(s_empty, "")
    S(s_dot, ".") S(k_O, "O") S(k_A, "A") S(k_G, "G") S(s_sqlglot, "sqlglot")
#undef S
    return 0;
}

static PyObject *setup(PyObject *mod, PyObject *args)
{
    PyObject *prim, *en, *dumps;
    if (!PyArg_ParseTuple(args, "O!OO", &PyDict_Type, &prim, &en, &dumps)) return NULL;
    PyObject *types[7] = {(PyObject *)&PyUnicode_Type, (PyObject *)&PyLong_Type, (PyObject *)&PyFloat_Type,
                          (PyObject *)&PyBool_Type, (PyObject *)&PyBytes_Type, (PyObject *)&PyComplex_Type,
                          (PyObject *)Py_TYPE(Py_None)};
    PyObject **slots[7] = {&n_str, &n_int, &n_float, &n_bool, &n_bytes, &n_complex, &n_none};
    if (PyDict_GET_SIZE(prim) != 7) { PyErr_SetString(PyExc_ValueError, "primitive table"); return NULL; }
    for (int i = 0; i < 7; i++) {
        PyObject *n = PyDict_GetItemWithError(prim, types[i]);
        if (!n) { if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "primitive table"); return NULL; }
        Py_XSETREF(*slots[i], Py_NewRef(n));
    }
    if (hash_selftest() < 0) return NULL;
    Py_XSETREF(prim_dict, Py_NewRef(prim));
    Py_XSETREF(enum_class, Py_NewRef(en));
    Py_XSETREF(type_enum, PyTuple_Pack(2, (PyObject *)&PyType_Type, en));
    if (!type_enum) return NULL;
    Py_XSETREF(func_types, PyTuple_Pack(5, (PyObject *)&PyFunction_Type, (PyObject *)&PyClassMethod_Type,
                                        (PyObject *)&PyStaticMethod_Type, (PyObject *)&PyProperty_Type,
                                        (PyObject *)&PyMethod_Type));
    if (!func_types) return NULL;
    Py_XSETREF(marshal_dumps, Py_NewRef(dumps));
    Py_RETURN_NONE;
}

static PyObject *hash_ok_get(PyObject *m, PyObject *unused) { return PyLong_FromLong(hash_ok); }
static PyObject *hash_ok_set(PyObject *m, PyObject *arg)
{
    int on = PyObject_IsTrue(arg);
    if (on < 0) return NULL;
    if (!on) hash_ok = 0;
    else if (hash_selftest() < 0) return NULL;
    Py_RETURN_NONE;
}

static PyObject *plain_meta_py(PyObject *m, PyObject *arg)
{
    if (!PyType_Check(arg)) { PyErr_SetString(PyExc_TypeError, "a type"); return NULL; }
    return PyBool_FromLong(plain_meta((PyTypeObject *)arg));
}

static PyMethodDef methods[] = {
    {"plain_meta", plain_meta_py, METH_O, "1 when the metaclass of t compares and hashes as type (the C tests apply)"},
    {"setup", setup, METH_VARARGS, "setup(_PRIM_NAME, enum.Enum, marshal.dumps)"},
    {"hash_ok", hash_ok_get, METH_NOARGS, "1 when tuples are hashed without being built"},
    {"set_hash_ok", hash_ok_set, METH_O, "turns the hashing without tuples off (0) or on (1, after the self-test)"},
    {NULL, NULL, 0, NULL},
};

static int intern_all(void)
{
#define S(var, text) if (!(var = PyUnicode_InternFromString(text))) return -1;
    S(k_cycle, "cycle") S(k_m, "m") S(k_d, "d") S(k_tc, "tc") S(k_s, "s") S(k_E, "E") S(k_C, "C") S(k_M, "M")
    S(k_F, "F") S(k_prop, "prop") S(k_meth, "meth")
    S(a_name, "__name__") S(a_module, "__module__") S(a_qualname, "__qualname__") S(a_value, "value") S(a_ename, "name")
    S(a_func, "__func__") S(a_self, "__self__") S(a_fget, "fget") S(a_fset, "fset") S(a_fdel, "fdel")
    S(a_code, "__code__") S(a_defaults, "__defaults__") S(a_kwdefaults, "__kwdefaults__")
    S(a_closure, "__closure__") S(a_cell_contents, "cell_contents") S(m_code, "code") S(m_rest, "_fp_rest")
    S(m_head, "_fp_head") S(m_fp, "_fp") S(m_fp_plain, "_fp_plain")
    S(m_items, "items")
    S(a_co_code, "co_code") S(a_co_names, "co_names") S(a_co_varnames, "co_varnames") S(a_co_freevars, "co_freevars")
    S(a_co_argcount, "co_argcount") S(a_co_flags, "co_flags") S(a_co_consts, "co_consts")
#undef S
    PyObject *ts[7] = {(PyObject *)&PyDict_Type, (PyObject *)&PyList_Type, (PyObject *)&PyTuple_Type,
                       (PyObject *)&PySet_Type, (PyObject *)&PyFrozenSet_Type, (PyObject *)&PyStaticMethod_Type,
                       (PyObject *)&PyClassMethod_Type};
    PyObject **names[7] = {&t_dict, &t_list, &t_tuple, &t_set, &t_frozenset, &t_staticmethod, &t_classmethod};
    for (int i = 0; i < 7; i++)
        if (!(*names[i] = PyObject_GetAttr(ts[i], a_name))) return -1;
    PyObject *e = PyUnicode_InternFromString("empty");
    if (!e) return -1;
    empty_cell = PyTuple_Pack(1, e);
    Py_DECREF(e);
    if (!empty_cell) return -1;
    two = PyLong_FromLong(2);
    return two ? 0 : -1;
}

static struct PyModuleDef moddef = {PyModuleDef_HEAD_INIT, "_statefpc", NULL, -1, methods};

PyMODINIT_FUNC PyInit__statefpc(void)
{
    if (intern_all() < 0 || adder_init() < 0) return NULL;
    if (PyType_Ready(&FPType) < 0 || PyType_Ready(&AdderType) < 0) return NULL;
    PyObject *m = PyModule_Create(&moddef);
    if (!m) return NULL;
    if (PyModule_AddObjectRef(m, "FP", (PyObject *)&FPType) < 0 || PyModule_AddObjectRef(m, "Adder", (PyObject *)&AdderType) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
