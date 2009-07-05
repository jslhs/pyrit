/*
#
#    Copyright 2008, 2009, Lukas Lueg, knabberknusperhaus@yahoo.de
#    Copyright 2009, Benedikt Heinz, Zn000h@googlemail.com
#
#    This file is part of Pyrit.
#
#    Pyrit is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Pyrit is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Pyrit.  If not, see <http://www.gnu.org/licenses/>.
*/

#include <Python.h>
#include <dlfcn.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>
#include "_stream.h" // Generated by preprocessing cpyrit_stream.br
#include "brook/Device.h"

typedef struct
{
    PyObject_HEAD
} StreamDevice;

int StreamDevCount;
brook::Device* StreamDevices;

extern "C" PyObject*
cpyrit_solve(PyObject * self, PyObject * args)
{
    char *essid_pre, essid[33+4], *passwd;
    unsigned char pad[64], temp[32];
    int i, slen;
    PyObject *passwd_seq, *passwd_obj, *result;
    SHA_CTX ctx_pad;
    unsigned int *dbuf, arraysize;

    if (!PyArg_ParseTuple (args, "sO", &essid_pre, &passwd_seq)) return NULL;
    passwd_seq = PyObject_GetIter(passwd_seq);
    if (!passwd_seq) return NULL;

    memset(essid, 0, sizeof (essid));
    slen = strlen(essid_pre);
    slen = slen <= 32 ? slen : 32;
    memcpy(essid, essid_pre, slen);
    slen = strlen (essid) + 4;
    
    dbuf = (unsigned int*)PyMem_Malloc(8192 * 2 * 4 * (5 * 3));
    if (dbuf == NULL)
    {
        Py_DECREF(passwd_seq);
        return PyErr_NoMemory();
    }

    arraysize = 0;
    while ((passwd_obj = PyIter_Next(passwd_seq)))
    {
        if (arraysize > 8192)
        {
            Py_DECREF(passwd_seq);
            PyMem_Free(dbuf);
            PyErr_SetString(PyExc_ValueError, "Sequence must not be longer than 8192 elements.");
            return NULL;
        }
        passwd = PyString_AsString(passwd_obj);
        if (passwd == NULL || strlen(passwd) < 8 || strlen(passwd) > 63)
        {
            Py_DECREF(passwd_seq);
            PyMem_Free(dbuf);
            PyErr_SetString(PyExc_ValueError, "All items must be strings between 8 and 63 characters");
            return NULL;
        }
        
        strncpy((char*)pad, passwd, sizeof(pad));
        for (i = 0; i < 16; i++)
            ((unsigned int*)pad)[i] ^= 0x36363636;
        SHA1_Init(&ctx_pad);
        SHA1_Update(&ctx_pad, pad, sizeof(pad));
        dbuf[(8192 * 2 * 0) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 0) + (arraysize * 2) + 0] = ctx_pad.h0;
        dbuf[(8192 * 2 * 1) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 1) + (arraysize * 2) + 0] = ctx_pad.h1;
        dbuf[(8192 * 2 * 2) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 2) + (arraysize * 2) + 0] = ctx_pad.h2;
        dbuf[(8192 * 2 * 3) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 3) + (arraysize * 2) + 0] = ctx_pad.h3;
        dbuf[(8192 * 2 * 4) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 4) + (arraysize * 2) + 0] = ctx_pad.h4;

        for (i = 0; i < 16; i++)
            ((unsigned int*)pad)[i] ^= 0x6A6A6A6A;
        SHA1_Init (&ctx_pad);
        SHA1_Update (&ctx_pad, pad, sizeof(pad));
        dbuf[(8192 * 2 * 5) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 5) + (arraysize * 2) + 0] = ctx_pad.h0;
        dbuf[(8192 * 2 * 6) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 6) + (arraysize * 2) + 0] = ctx_pad.h1;
        dbuf[(8192 * 2 * 7) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 7) + (arraysize * 2) + 0] = ctx_pad.h2;
        dbuf[(8192 * 2 * 8) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 8) + (arraysize * 2) + 0] = ctx_pad.h3;
        dbuf[(8192 * 2 * 9) + (arraysize * 2) + 1] = dbuf[(8192 * 2 * 9) + (arraysize * 2) + 0] = ctx_pad.h4;

        essid[slen - 1] = '\1';
        HMAC(EVP_sha1(), (unsigned char*)passwd, strlen(passwd), (unsigned char*)essid, slen, (unsigned char*)&ctx_pad, NULL);
        dbuf[(8192 * 2 * 10) + (arraysize * 2) + 0] = ctx_pad.h0;
        dbuf[(8192 * 2 * 11) + (arraysize * 2) + 0] = ctx_pad.h1;
        dbuf[(8192 * 2 * 12) + (arraysize * 2) + 0] = ctx_pad.h2;
        dbuf[(8192 * 2 * 13) + (arraysize * 2) + 0] = ctx_pad.h3;
        dbuf[(8192 * 2 * 14) + (arraysize * 2) + 0] = ctx_pad.h4;

        essid[slen - 1] = '\2';
        HMAC(EVP_sha1(), (unsigned char*)passwd, strlen(passwd), (unsigned char*)essid, slen, (unsigned char*)&ctx_pad, NULL);
        dbuf[(8192 * 2 * 10) + (arraysize * 2) + 1] = ctx_pad.h0;
        dbuf[(8192 * 2 * 11) + (arraysize * 2) + 1] = ctx_pad.h1;
        dbuf[(8192 * 2 * 12) + (arraysize * 2) + 1] = ctx_pad.h2;
        dbuf[(8192 * 2 * 13) + (arraysize * 2) + 1] = ctx_pad.h3;
        dbuf[(8192 * 2 * 14) + (arraysize * 2) + 1] = ctx_pad.h4;
        
        arraysize++;
    }
    Py_DECREF(passwd_seq);

    Py_BEGIN_ALLOW_THREADS;

    ::brook::Stream < uint2 > ipad_A (1, &arraysize);
    ::brook::Stream < uint2 > ipad_B (1, &arraysize);
    ::brook::Stream < uint2 > ipad_C (1, &arraysize);
    ::brook::Stream < uint2 > ipad_D (1, &arraysize);
    ::brook::Stream < uint2 > ipad_E (1, &arraysize);

    ::brook::Stream < uint2 > opad_A (1, &arraysize);
    ::brook::Stream < uint2 > opad_B (1, &arraysize);
    ::brook::Stream < uint2 > opad_C (1, &arraysize);
    ::brook::Stream < uint2 > opad_D (1, &arraysize);
    ::brook::Stream < uint2 > opad_E (1, &arraysize);

    ::brook::Stream < uint2 > pmk_in0 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_in1 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_in2 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_in3 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_in4 (1, &arraysize);

    ::brook::Stream < uint2 > pmk_out0 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_out1 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_out2 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_out3 (1, &arraysize);
    ::brook::Stream < uint2 > pmk_out4 (1, &arraysize);

    ipad_A.read (dbuf + (8192 * 2 * 0));
    ipad_B.read (dbuf + (8192 * 2 * 1));
    ipad_C.read (dbuf + (8192 * 2 * 2));
    ipad_D.read (dbuf + (8192 * 2 * 3));
    ipad_E.read (dbuf + (8192 * 2 * 4));

    opad_A.read (dbuf + (8192 * 2 * 5));
    opad_B.read (dbuf + (8192 * 2 * 6));
    opad_C.read (dbuf + (8192 * 2 * 7));
    opad_D.read (dbuf + (8192 * 2 * 8));
    opad_E.read (dbuf + (8192 * 2 * 9));

    pmk_in0.read (dbuf + (8192 * 2 * 10));
    pmk_in1.read (dbuf + (8192 * 2 * 11));
    pmk_in2.read (dbuf + (8192 * 2 * 12));
    pmk_in3.read (dbuf + (8192 * 2 * 13));
    pmk_in4.read (dbuf + (8192 * 2 * 14));

    sha1_rounds (ipad_A, ipad_B, ipad_C, ipad_D, ipad_E, opad_A, opad_B, opad_C,
        opad_D, opad_E, pmk_in0, pmk_in1, pmk_in2, pmk_in3, pmk_in4,
        pmk_out0, pmk_out1, pmk_out2, pmk_out3, pmk_out4, uint2 (0x80000000, 0x80000000));

    pmk_out0.write (dbuf + (8192 * 2 * 0));
    pmk_out1.write (dbuf + (8192 * 2 * 1));
    pmk_out2.write (dbuf + (8192 * 2 * 2));
    pmk_out3.write (dbuf + (8192 * 2 * 3));
    pmk_out4.write (dbuf + (8192 * 2 * 4));

    i = 0;
    if ((pmk_out0.error () !=::brook::BR_NO_ERROR)
        || (pmk_out1.error () !=::brook::BR_NO_ERROR)
        || (pmk_out2.error () !=::brook::BR_NO_ERROR)
        || (pmk_out3.error () !=::brook::BR_NO_ERROR)
        || (pmk_out4.error () !=::brook::BR_NO_ERROR))
            i = -1;

    Py_END_ALLOW_THREADS;

    if (i)
    {
        PyErr_SetString(PyExc_SystemError, "Kernel-call failed in AMD-Stream core.");
        free(dbuf);
        return NULL;
    }

    result = PyTuple_New(arraysize);
    for (i = 0; i < (int)arraysize * 2; i++)
    {
        temp[0] = dbuf[(0 * 8192 * 2) + i];
        temp[1] = dbuf[(1 * 8192 * 2) + i];
        temp[2] = dbuf[(2 * 8192 * 2) + i];
        temp[3] = dbuf[(3 * 8192 * 2) + i];
        temp[4] = dbuf[(4 * 8192 * 2) + i];
        i++;
        temp[5] = dbuf[(0 * 8192 * 2) + i];
        temp[6] = dbuf[(1 * 8192 * 2) + i];
        temp[7] = dbuf[(2 * 8192 * 2) + i];
        PyTuple_SetItem(result, i / 2, Py_BuildValue ("s#", temp, 32));
    }

    PyMem_Free(dbuf);

    return result;
}

PyObject*
cpyrit_getDevCount(PyObject* self, PyObject* args)
{
    if (!PyArg_ParseTuple(args, ""))
        return NULL;
    return Py_BuildValue("i", StreamDevCount);
}

PyObject*
cpyrit_setDevice(PyObject* self, PyObject* args)
{
    int StreamDev;
    if (!PyArg_ParseTuple(args, "i", &StreamDev)) return NULL;

    if (StreamDev < 0 || StreamDev > StreamDevCount-1)
    {
        PyErr_SetString(PyExc_SystemError, "Invalid device number");
        return NULL;
    }

    brook::useDevices(StreamDevices + StreamDev, 1, NULL);

    return Py_None;
}

static PyMethodDef StreamDevice_methods[] =
{
    {"solve", (PyCFunction)cpyrit_solve, METH_VARARGS, "Calculate PMKs from ESSID and list of strings."},
    {NULL, NULL}
};

static PyTypeObject StreamDevice_type = {
    PyObject_HEAD_INIT(NULL)
    0,                          /*ob_size*/
    "_cpyrit_stream.StreamDevice",/*tp_name*/
    sizeof(StreamDevice),       /*tp_basicsize*/
    0,                          /*tp_itemsize*/
    0,                          /*tp_dealloc*/
    0,                          /*tp_print*/
    0,                          /*tp_getattr*/
    0,                          /*tp_setattr*/
    0,                          /*tp_compare*/
    0,                          /*tp_repr*/
    0,                          /*tp_as_number*/
    0,                          /*tp_as_sequence*/
    0,                          /*tp_as_mapping*/
    0,                          /*tp_hash*/
    0,                          /*tp_call*/
    0,                          /*tp_str*/
    0,                          /*tp_getattro*/
    0,                          /*tp_setattro*/
    0,                          /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT          /*tp_flags*/
     | Py_TPFLAGS_BASETYPE,
    0,                          /*tp_doc*/
    0,                          /*tp_traverse*/
    0,                          /*tp_clear*/
    0,                          /*tp_richcompare*/
    0,                          /*tp_weaklistoffset*/
    0,                          /*tp_iter*/
    0,                          /*tp_iternext*/
    StreamDevice_methods,       /*tp_methods*/
    0,                          /*tp_members*/
    0,                          /*tp_getset*/
    0,                          /*tp_base*/
    0,                          /*tp_dict*/
    0,                          /*tp_descr_get*/
    0,                          /*tp_descr_set*/
    0,                          /*tp_dictoffset*/
    0,                          /*tp_init*/
    0,                          /*tp_alloc*/
    0,                          /*tp_new*/
    0,                          /*tp_free*/
    0,                          /*tp_is_gc*/
};

static PyMethodDef CPyritStreamMethods[] = {
    {"setDevice", cpyrit_setDevice, METH_VARARGS, "Binds the current thread to the given device."},
    {"getDeviceCount", cpyrit_getDevCount, METH_VARARGS, "Returns the number of available CAL-devices."},
    {NULL, NULL, 0, NULL}
};

PyMODINIT_FUNC
init_cpyrit_stream(void)
{
    PyObject *m;

    // The whole purpose is to prevent Brook/SystemRT.cpp from complaining about this to stdout
    void* libHndl = dlopen("libaticalcl.so", RTLD_NOW);
    if(libHndl)
    {
        dlclose(libHndl);
    }
    else
    {
        PyErr_SetString(PyExc_ImportError, "libaticalcl.so not found.");
        return;
    }

    StreamDevices = brook::getDevices("cal", (unsigned int*)&StreamDevCount);
    if (StreamDevCount < 1)
    {
        PyErr_SetString(PyExc_ImportError, "No CAL-compatible devices available.");
        return;
    }

    StreamDevice_type.tp_getattro = PyObject_GenericGetAttr;
    StreamDevice_type.tp_setattro = PyObject_GenericSetAttr;
    StreamDevice_type.tp_alloc  = PyType_GenericAlloc;
    StreamDevice_type.tp_new = PyType_GenericNew;
    StreamDevice_type.tp_free = _PyObject_Del;  
    if (PyType_Ready(&StreamDevice_type) < 0)
	    return;

    m = Py_InitModule("_cpyrit_stream", CPyritStreamMethods);

    Py_INCREF(&StreamDevice_type);
    PyModule_AddObject(m, "StreamDevice", (PyObject*)&StreamDevice_type);
}

