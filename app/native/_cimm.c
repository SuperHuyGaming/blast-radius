/* _cimm: the pass of procmodel._immortalize() in C (same writes, same order, same exceptions).

   immortalize(objs, imm) -> n
     objs: the list from gc.get_objects(...) (any list); imm: the 32-bit value to write.
   As the Python pass:
     1. codes = [o.__code__ for o in objs if type(o) is FunctionType]   (what func_get_code does:
        the same audit event "object.__getattr__" per function, then func_code)
     (every id() call of the Python pass is matched by its audit event "builtins.id", at the same point)
     2. for each object of objs, in order: the low 32 bits of its reference count := imm
        (the memoryview of procmodel covers addresses below 2**47: an object above that raises
        IndexError there, as the memoryview store does)
     3. while codes: c = codes.pop() (last first); skipped if already seen (by address); else
        written, counted, and each constant: a code object (exact type) is pushed, any other
        object written.
   Returns len(objs) + the number of distinct code objects walked. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

#define LIMIT ((uintptr_t)1 << 47)   /* (1 << 45) uint32 words from address 0 */


typedef struct { uintptr_t *slots; size_t mask; size_t used; } ptrset;

static int ps_init(ptrset *s, size_t hint) {
    size_t cap = 1024;
    while (cap < hint * 2) cap <<= 1;
    s->slots = (uintptr_t *)PyMem_Calloc(cap, sizeof(uintptr_t));
    if (!s->slots) { PyErr_NoMemory(); return -1; }
    s->mask = cap - 1; s->used = 0;
    return 0;
}

static size_t ps_hash(uintptr_t p) { p >>= 4; p *= (uintptr_t)0x9E3779B97F4A7C15ULL; return (size_t)(p ^ (p >> 29)); }

/* 1 when added, 0 when it was there, -1 on error */
static int ps_add(ptrset *s, uintptr_t p) {
    if ((s->used + 1) * 2 > s->mask + 1) {
        size_t ncap = (s->mask + 1) * 2;
        uintptr_t *ns = (uintptr_t *)PyMem_Calloc(ncap, sizeof(uintptr_t));
        if (!ns) { PyErr_NoMemory(); return -1; }
        for (size_t i = 0; i <= s->mask; i++) {
            uintptr_t q = s->slots[i];
            if (q) { size_t j = ps_hash(q) & (ncap - 1); while (ns[j]) j = (j + 1) & (ncap - 1); ns[j] = q; }
        }
        PyMem_Free(s->slots); s->slots = ns; s->mask = ncap - 1;
    }
    size_t j = ps_hash(p) & s->mask;
    while (s->slots[j]) { if (s->slots[j] == p) return 0; j = (j + 1) & s->mask; }
    s->slots[j] = p; s->used++;
    return 1;
}

static inline int put(PyObject *o, uint32_t imm) {
    uintptr_t a = (uintptr_t)o;
    if (a >= LIMIT) { PyErr_SetString(PyExc_IndexError, "index out of bounds on dimension 1"); return -1; }
    uint32_t *w = (uint32_t *)(a & ~(uintptr_t)3);
    /* the same word as the store of the Python pass; a word that holds imm already is left
       unwritten (same contents; its page is not copied when a fork shares it) */
    if (*w != imm)
        *w = imm;
    return 0;
}

/* a stack of strong references */
typedef struct { PyObject **v; Py_ssize_t n, cap; } stack;

static int st_push(stack *s, PyObject *o) {
    if (s->n == s->cap) {
        Py_ssize_t ncap = s->cap ? s->cap * 2 : 4096;
        PyObject **nv = (PyObject **)PyMem_Realloc(s->v, (size_t)ncap * sizeof(PyObject *));
        if (!nv) { PyErr_NoMemory(); return -1; }
        s->v = nv; s->cap = ncap;
    }
    s->v[s->n++] = Py_NewRef(o);
    return 0;
}

static void st_free(stack *s) {
    while (s->n > 0) { s->n--; Py_DECREF(s->v[s->n]); }
    PyMem_Free(s->v);
}

/* Audit events. The Python pass raises "object.__getattr__" (f, "__code__") for each function
   (funcobject.c func_get_code) and "builtins.id" (id,) for each id() call (bltinmodule.c
   builtin_id, id = PyLong_FromVoidPtr(o)): the C pass raises the same events at the same points.
   Whether anything listens is learned from the first event of the pass: its arguments are made
   by a converter ("O&"), which PySys_Audit calls only when a hook (or the DTrace probe) is on.
   If it was not called, none is installed, and none can be installed during the pass: no Python
   code runs in it but audit hooks (decrefs free nothing: objs holds every object, and the code
   objects are immortal once written). Then the other events are skipped (they would build nothing
   and call nothing). */
static int hooks_called;

static PyObject *conv_obj(void *p) {
    hooks_called = 1;
    return Py_NewRef((PyObject *)p);
}

static PyObject *conv_id(void *p) {
    hooks_called = 1;
    return PyLong_FromVoidPtr(p);
}

/* mode: -1 not known yet, 0 no hook, 1 hooks */
static inline int audit_getattr(PyObject *f, int *mode) {
    if (*mode == 0) return 0;
    hooks_called = 0;
    int r = PySys_Audit("object.__getattr__", "O&s", conv_obj, (void *)f, "__code__");
    if (*mode < 0) *mode = hooks_called;
    return r;
}

static inline int audit_id(PyObject *o, int *mode) {
    if (*mode == 0) return 0;
    hooks_called = 0;
    int r = PySys_Audit("builtins.id", "O&", conv_id, (void *)o);
    if (*mode < 0) *mode = hooks_called;
    return r;
}

static PyObject *immortalize(PyObject *self, PyObject *args) {
    PyObject *objs; unsigned long long imm_ll;
    if (!PyArg_ParseTuple(args, "O!K", &PyList_Type, &objs, &imm_ll)) return NULL;
    if (imm_ll > 0xFFFFFFFFULL) { PyErr_SetString(PyExc_ValueError, "imm out of range"); return NULL; }
    uint32_t imm = (uint32_t)imm_ll;
    Py_INCREF(objs);
    stack codes = {NULL, 0, 0};
    Py_ssize_t i, n = 0, walked = 0;
    ptrset seen = {NULL, 0, 0};
    int mode = -1;
    /* 1. codes = [o.__code__ for o in objs if type(o) is FunctionType]. For an object of exactly
       that type, o.__code__ is funcobject.c's func_get_code (the type is immutable and cannot be
       subclassed, the getset is a data descriptor, so the instance dict is not looked at): the
       audit event "object.__getattr__" (op, "__code__"), then a new reference to func_code. */
    for (i = 0; i < PyList_GET_SIZE(objs); i++) {
        PyObject *o = PyList_GET_ITEM(objs, i);
        if (Py_IS_TYPE(o, &PyFunction_Type)) {
            Py_INCREF(o);
            if (audit_getattr(o, &mode) < 0) { Py_DECREF(o); goto fail; }
            int r = st_push(&codes, ((PyFunctionObject *)o)->func_code);
            Py_DECREF(o);
            if (r < 0) goto fail;
        }
    }
    /* 2. every object of objs, in order */
    for (i = 0; i < PyList_GET_SIZE(objs); i++) {
        PyObject *o = PyList_GET_ITEM(objs, i);
        if (audit_id(o, &mode) < 0 || put(o, imm) < 0) goto fail;
    }
    n = PyList_GET_SIZE(objs);
    /* 3. the code objects, last first */
    if (ps_init(&seen, (size_t)codes.n) < 0) goto fail;
    while (codes.n > 0) {
        PyObject *c = codes.v[--codes.n];   /* the reference moves to c */
        if (audit_id(c, &mode) < 0) { Py_DECREF(c); goto fail; }   /* ci = id(c) */
        int added = ps_add(&seen, (uintptr_t)c);
        if (added < 0) { Py_DECREF(c); goto fail; }
        if (added == 0) { Py_DECREF(c); continue; }
        if (put(c, imm) < 0) { Py_DECREF(c); goto fail; }
        walked++;
        PyObject *consts = ((PyCodeObject *)c)->co_consts;
        for (Py_ssize_t k = 0; k < PyTuple_GET_SIZE(consts); k++) {
            PyObject *x = PyTuple_GET_ITEM(consts, k);
            if (Py_IS_TYPE(x, &PyCode_Type)) {
                if (st_push(&codes, x) < 0) { Py_DECREF(c); goto fail; }
            } else if (audit_id(x, &mode) < 0 || put(x, imm) < 0) { Py_DECREF(c); goto fail; }
        }
        Py_DECREF(c);
    }
    PyMem_Free(seen.slots);
    st_free(&codes);
    Py_DECREF(objs);
    return PyLong_FromSsize_t(n + walked);
fail:
    if (seen.slots) PyMem_Free(seen.slots);
    st_free(&codes);
    Py_DECREF(objs);
    return NULL;
}

/* code_of(f): f's code object, read from the function (no audit event, unlike f.__code__ and
   gc.get_referents): for procmodel's check of the words, which must raise no event of its own */
static PyObject *code_of(PyObject *self, PyObject *f) {
    if (!Py_IS_TYPE(f, &PyFunction_Type)) { PyErr_SetString(PyExc_TypeError, "a function"); return NULL; }
    return Py_NewRef(((PyFunctionObject *)f)->func_code);
}

static PyMethodDef methods[] = {
    {"immortalize", immortalize, METH_VARARGS, "procmodel._immortalize's pass over a list of objects"},
    {"code_of", code_of, METH_O, "a function's code object, without the audit event"},
    {NULL, NULL, 0, NULL}
};

/* multi-phase init: loading it puts nothing in sys.modules */
static int cimm_exec(PyObject *m) {
    return 0;
}

static PyModuleDef_Slot slots[] = {
    {Py_mod_exec, cimm_exec},
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {0, NULL}
};

static struct PyModuleDef moddef = {PyModuleDef_HEAD_INIT, "_cimm", NULL, 0, methods, slots, NULL, NULL, NULL};

PyMODINIT_FUNC PyInit__cimm(void) {
    return PyModuleDef_Init(&moddef);
}
