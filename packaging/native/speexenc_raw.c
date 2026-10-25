/*
 * speexenc_raw - encode mono 16 bit PCM into raw Speex frames.
 *
 * Why this exists
 * ---------------
 * The ETS100 cloud expects the recording of every answer twice: as an mp3
 * (playback) and as a raw Speex payload (the "spx" attachment).  ffmpeg used
 * to ship the ``libspeex`` encoder, but it was removed in ffmpeg 6.0 - every
 * ffmpeg that is current today (including the static binary FastETS bundles)
 * simply cannot produce Speex any more.
 *
 * This helper is a tiny command line front end for libspeex
 * (https://github.com/xiph/speex, BSD licensed, see LICENSE.libspeex).  It is
 * statically linked against libspeex so that FastETS has a working Speex
 * encoder on macOS and Windows without asking the user to install anything.
 *
 * It mirrors what ffmpeg's libspeex encoder does, so the frames are byte
 * identical to the ones ffmpeg produces:
 *
 *   - input:  mono signed 16 bit little endian PCM (as ffmpeg's
 *             AV_SAMPLE_FMT_S16 encoder input)
 *   - encode: speex_encode_int() with the same quality / complexity defaults
 *   - output: the raw Speex frames, i.e. exactly what ffmpeg's Ogg Speex
 *             muxer would have put into the Ogg packets
 *
 * Usage
 * -----
 *   speexenc_raw [--rate 16000] [--quality 8] [--vbr 0] [--vbr-quality 0.2]
 *                 [--complexity 3] [--lengths-file PATH] < pcm.s16le > frames.speex
 *   speexenc_raw --selftest
 */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "speex/speex.h"
#include "speex/speex_bits.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define MAX_FRAME_BYTES 1024
/* Upper bound for the frame size libspeex may report (16 kHz wideband = 320). */
#define MAX_FRAME_SAMPLES 2048

static void usage(void)
{
    fprintf(stderr,
            "usage: speexenc_raw [--rate N] [--quality N] [--vbr 0|1]\n"
            "                     [--vbr-quality F] [--complexity N]\n"
            "                     [--lengths-file PATH] [--selftest]\n");
}

/* Pick the Speex mode that matches the sampling rate. */
static const SpeexMode *mode_for_rate(int rate)
{
    if (rate <= 8000) {
        return &speex_nb_mode;
    }
    if (rate <= 16000) {
        return &speex_wb_mode;
    }
    return &speex_uwb_mode;
}

static void *make_encoder(const SpeexMode *mode, int rate, int quality,
                          int vbr, double vbr_quality, int complexity)
{
    void *state = speex_encoder_init(mode);
    if (!state) {
        return NULL;
    }
    speex_encoder_ctl(state, SPEEX_SET_SAMPLING_RATE, &rate);
    speex_encoder_ctl(state, SPEEX_SET_QUALITY, &quality);
    if (complexity > 0) {
        speex_encoder_ctl(state, SPEEX_SET_COMPLEXITY, &complexity);
    }
    if (vbr) {
        int vbr_on = 1;
        float constraint = (float)vbr_quality;
        speex_encoder_ctl(state, SPEEX_SET_VBR, &vbr_on);
        speex_encoder_ctl(state, SPEEX_SET_VBR_QUALITY, &constraint);
    } else {
        int vbr_off = 0;
        speex_encoder_ctl(state, SPEEX_SET_VBR, &vbr_off);
    }
    return state;
}

/*
 * Encode one frame of 16 bit PCM and return the number of bytes written.
 *
 * The bit buffer is reused for every frame and is *not* rewound before the
 * bytes are taken out: ``speex_bits_write`` inserts the terminator at the
 * current position, which is what pads the last byte of a frame with ones.
 * ffmpeg's libspeex encoder does exactly the same, so the frames produced
 * here are byte identical to the ones ffmpeg produces.
 */
static int encode_frame(void *state, const spx_int16_t *pcm, char *out,
                         int max_len, SpeexBits *bits)
{
    int ret;
    int written;

    speex_bits_reset(bits);
    ret = speex_encode_int(state, (spx_int16_t *)pcm, bits);
    if (ret != 1) {
        return 0; /* DTX: nothing to transmit for this frame */
    }
    written = speex_bits_write(bits, out, max_len);
    if (written < 0) {
        return 0;
    }
    return written;
}

static int run_encode(int rate, int quality, int vbr, double vbr_quality,
                       int complexity, const char *lengths_file)
{
    const SpeexMode *mode = mode_for_rate(rate);
    void *state;
    int frame_size = 0;
    spx_int16_t *pcm;
    char out[MAX_FRAME_BYTES];
    size_t got;
    FILE *lengths = NULL;
    SpeexBits bits;

    if (lengths_file) {
        lengths = fopen(lengths_file, "w");
        if (!lengths) {
            fprintf(stderr, "speexenc_raw: cannot write %s\n", lengths_file);
            return 1;
        }
    }

    state = make_encoder(mode, rate, quality, vbr, vbr_quality, complexity);
    if (!state) {
        fprintf(stderr, "speexenc_raw: cannot initialise the encoder\n");
        if (lengths) fclose(lengths);
        return 1;
    }
    speex_encoder_ctl(state, SPEEX_GET_FRAME_SIZE, &frame_size);
    if (frame_size <= 0 || frame_size > MAX_FRAME_SAMPLES) {
        fprintf(stderr, "speexenc_raw: invalid frame size %d\n", frame_size);
        speex_encoder_destroy(state);
        if (lengths) fclose(lengths);
        return 1;
    }
    pcm = (spx_int16_t *)calloc((size_t)frame_size, sizeof(spx_int16_t));
    if (!pcm) {
        speex_encoder_destroy(state);
        if (lengths) fclose(lengths);
        return 1;
    }
    speex_bits_init(&bits);

    while ((got = fread(pcm, sizeof(spx_int16_t), (size_t)frame_size, stdin)) > 0) {
        int n;
        if (got < (size_t)frame_size) {
            /* ffmpeg's encoder never emits an incomplete frame, so the
               trailing partial frame is dropped here as well. */
            break;
        }
        n = encode_frame(state, pcm, out, (int)sizeof(out), &bits);
        if (n > 0) {
            fwrite(out, 1, (size_t)n, stdout);
            if (lengths) {
                fprintf(lengths, "%d\n", n);
            }
        }
    }

    speex_bits_destroy(&bits);
    free(pcm);
    speex_encoder_destroy(state);
    if (lengths) {
        fclose(lengths);
    }
    fflush(stdout);
    return 0;
}

/*
 * Round trip check: synthesise a sine wave, encode it, decode it again and
 * report the signal to noise ratio.  A healthy encoder reaches a decent SNR;
 * a broken one (wrong input scale, truncated frames, ...) does not.
 */
static int run_selftest(void)
{
    int rate = 16000;
    const int frames = 50;
    const SpeexMode *mode = mode_for_rate(rate);
    void *enc = NULL;
    void *dec = NULL;
    int frame_size = 0;
    spx_int16_t *pcm = NULL;
    float *decoded = NULL;
    double *reference = NULL;
    double *restored = NULL;
    SpeexBits ebits;
    int total;
    double snr;
    int f;
    int i;
    int ok = 0;
    const double amplitude = 16383.0; /* half of the int16 range */

    total = frames * MAX_FRAME_SAMPLES;
    reference = (double *)calloc((size_t)total, sizeof(double));
    restored = (double *)calloc((size_t)total, sizeof(double));
    if (!reference || !restored) {
        free(reference);
        free(restored);
        return 1;
    }

    {
        double scale = 1.0;
        double best_snr = -999.0;
        int best_lag = 0;
        int lag;

        enc = make_encoder(mode, rate, 8, 0, 0.2, 3);
        dec = speex_decoder_init(mode);
        if (!enc || !dec) {
            fprintf(stderr, "speexenc_raw: cannot initialise codec\n");
            if (enc) speex_encoder_destroy(enc);
            if (dec) speex_decoder_destroy(dec);
            free(reference);
            free(restored);
            return 1;
        }
        speex_decoder_ctl(dec, SPEEX_SET_SAMPLING_RATE, &rate);
        speex_encoder_ctl(enc, SPEEX_GET_FRAME_SIZE, &frame_size);
        speex_decoder_ctl(dec, SPEEX_GET_FRAME_SIZE, &frame_size);
        if (frame_size <= 0 || frame_size > MAX_FRAME_SAMPLES) {
            fprintf(stderr, "speexenc_raw: invalid frame size\n");
            speex_encoder_destroy(enc);
            speex_decoder_destroy(dec);
            free(reference);
            free(restored);
            return 1;
        }
        pcm = (spx_int16_t *)calloc((size_t)frame_size, sizeof(spx_int16_t));
        decoded = (float *)calloc((size_t)frame_size, sizeof(float));
        speex_bits_init(&ebits);
        if (!pcm || !decoded) {
            free(pcm);
            free(decoded);
            speex_encoder_destroy(enc);
            speex_decoder_destroy(dec);
            free(reference);
            free(restored);
            return 1;
        }
        memset(reference, 0, (size_t)total * sizeof(double));
        memset(restored, 0, (size_t)total * sizeof(double));

        for (f = 0; f < frames; f++) {
            char out[MAX_FRAME_BYTES];
            int n;
            SpeexBits rbits;

            for (i = 0; i < frame_size; i++) {
                double t = (double)(f * frame_size + i) / (double)rate;
                double value = amplitude * sin(2.0 * M_PI * 440.0 * t);
                pcm[i] = (spx_int16_t)value;
                reference[f * frame_size + i] = value;
            }
            n = encode_frame(enc, pcm, out, (int)sizeof(out), &ebits);
            if (n <= 0) {
                continue;
            }
            memset(decoded, 0, (size_t)frame_size * sizeof(float));
            speex_bits_init(&rbits);
            speex_bits_read_from(&rbits, out, n);
            speex_decode(dec, &rbits, decoded);
            speex_bits_destroy(&rbits);

            for (i = 0; i < frame_size; i++) {
                restored[f * frame_size + i] = (double)decoded[i];
            }
        }

        /* The decoder introduces a small delay - search for the best lag. */
        for (lag = 0; lag < 2 * frame_size && lag < total; lag++) {
            double signal_sum = 0.0;
            double noise_sum = 0.0;
            int count = total - lag;
            for (i = 0; i < count; i++) {
                double r = reference[i];
                double d = restored[i + lag] - reference[i];
                signal_sum += r * r;
                noise_sum += d * d;
            }
            if (signal_sum <= 0.0) {
                continue;
            }
            {
                double value = (noise_sum > 0.0)
                                   ? 10.0 * log10(signal_sum / noise_sum)
                                   : 99.0;
                if (value > best_snr) {
                    best_snr = value;
                    best_lag = lag;
                }
            }
        }

        free(pcm);
        free(decoded);
        speex_bits_destroy(&ebits);
        speex_encoder_destroy(enc);
        speex_decoder_destroy(dec);
        pcm = NULL;
        decoded = NULL;
        enc = NULL;
        dec = NULL;

        snr = best_snr;
        printf("snr=%.2f dB lag=%d\n", snr, best_lag);
        if (snr > 10.0) {
            ok = 1;
        }
    }

    free(reference);
    free(restored);
    if (!ok) {
        fprintf(stderr, "speexenc_raw: selftest failed\n");
        return 1;
    }
    printf("speexenc_raw: selftest ok\n");
    return 0;
}

int main(int argc, char **argv)
{
    int rate = 16000;
    int quality = 8;
    int vbr = 0;
    double vbr_quality = 0.2;
    int complexity = 3;
    const char *lengths_file = NULL;
    int i;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--rate") && i + 1 < argc) {
            rate = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--quality") && i + 1 < argc) {
            quality = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--vbr") && i + 1 < argc) {
            vbr = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--vbr-quality") && i + 1 < argc) {
            vbr_quality = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--complexity") && i + 1 < argc) {
            complexity = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--lengths-file") && i + 1 < argc) {
            lengths_file = argv[++i];
        } else if (!strcmp(argv[i], "--selftest")) {
            return run_selftest();
        } else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            usage();
            return 0;
        } else {
            fprintf(stderr, "speexenc_raw: unknown argument %s\n", argv[i]);
            usage();
            return 2;
        }
    }

    if (rate <= 0) {
        fprintf(stderr, "speexenc_raw: invalid sample rate\n");
        return 2;
    }
    return run_encode(rate, quality, vbr, vbr_quality, complexity, lengths_file);
}
