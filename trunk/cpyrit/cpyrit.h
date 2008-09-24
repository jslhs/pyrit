/*
#
#    Copyright 2008, Lukas Lueg, knabberknusperhaus@yahoo.de
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

#ifndef CPYRIT
#define CPYRIT

#include <python2.5/Python.h>
#include <stdint.h>
#include <pthread.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>
#ifdef HAVE_CUDA
    #include <cuda_runtime.h>
#endif

#define GET_BE(n,b,i)                            \
{                                                       \
    (n) = ( (uint32_t) (b)[(i)    ] << 24 )        \
        | ( (uint32_t) (b)[(i) + 1] << 16 )        \
        | ( (uint32_t) (b)[(i) + 2] <<  8 )        \
        | ( (uint32_t) (b)[(i) + 3]       );       \
}

#define PUT_BE(n,b,i)                            \
{                                                       \
    (b)[(i)    ] = (unsigned char) ( (n) >> 24 );       \
    (b)[(i) + 1] = (unsigned char) ( (n) >> 16 );       \
    (b)[(i) + 2] = (unsigned char) ( (n) >>  8 );       \
    (b)[(i) + 3] = (unsigned char) ( (n)       );       \
}

#ifdef HAVE_PADLOCK
    #include <sys/ucontext.h>
    #include <signal.h>
    #include <errno.h>
    #include <sys/mman.h>

    struct xsha1_ctx {
        unsigned int state[32];
        char inputbuffer[20+64];
    } __attribute__((aligned(16)));
#endif

struct thread_ctr {
    pthread_t thread_id;
    void* keyptr;
    unsigned int keycount;
    unsigned int keyoffset;
    unsigned int keystep;
    void* bufferptr;
    char* essid;
};

#ifdef HAVE_CUDA

    typedef struct {
        uint32_t h0,h1,h2,h3,h4;
    } SHA_DEV_CTX;

    #define CPY_DEVCTX(src, dst) \
    { \
        dst.h0 = src.h0; dst.h1 = src.h1; \
        dst.h2 = src.h2; dst.h3 = src.h3; \
        dst.h4 = src.h4; \
    }

    typedef struct {
        SHA_DEV_CTX ctx_ipad;
        SHA_DEV_CTX ctx_opad;
        SHA_DEV_CTX e1;
        SHA_DEV_CTX e2;
    } gpu_inbuffer;

    typedef struct {
        SHA_DEV_CTX pmk1;
        SHA_DEV_CTX pmk2;
    } gpu_outbuffer;

#endif

#endif
