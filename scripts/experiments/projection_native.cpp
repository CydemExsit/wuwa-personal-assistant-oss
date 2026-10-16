// Experimental C ABI kernel for measuring Python/native conversion and packaging.
// This is not loaded by the application. Compile without fast-math or contraction.
#include <cmath>
#include <cstdint>

#ifdef _WIN32
#define EXPORT extern "C" __declspec(dllexport)
#else
#define EXPORT extern "C"
#endif

using TieResolver = int (*)(double, double, double);

struct Projection {
    const double* points;
    const double* probabilities;
    const int* offsets;
    const int* sizes;
    int type_count;
    int remaining;
    double earned;
    double reference;
    double ideal;
    TieResolver resolve_tie;
    double* mass;
    int selected[5];
    double contributions[5];

    void outcomes(int depth, double probability) {
        if (depth < remaining) {
            int type = selected[depth];
            for (int j = 0; j < sizes[type]; ++j) {
                int index = offsets[type] + j;
                contributions[depth] = points[index];
                outcomes(depth + 1, probability * probabilities[index]);
            }
            return;
        }
        double sum = 0.0, correction = 0.0;
        for (int j = 0; j < remaining; ++j) {
            double value = contributions[j];
            double next = sum + value;
            correction += std::abs(sum) >= std::abs(value)
                ? (sum - next) + value : (value - next) + sum;
            sum = next;
        }
        double total = (sum + correction) + earned;
        double score = reference <= 0 ? 0.0
            : ideal <= reference + 1e-9 ? total / reference * 100.0
            : total <= reference ? total / reference * 90.0
            : 90.0 + (total - reference) / (ideal - reference) * 10.0;
        score = score < 0 ? 0 : score > 100 ? 100 : score;
        double scaled = score * 100.0;
        double spacing_input = scaled < 1 ? 1 : scaled;
        double spacing = std::nextafter(spacing_input, INFINITY) - spacing_input;
        int cents = std::abs(scaled - (std::floor(scaled) + 0.5)) <= 4 * spacing
            ? resolve_tie(total, reference, ideal)
            : static_cast<int>(std::nearbyint(scaled));
        mass[cents] += probability;
    }

    void subsets(int depth, int start, double probability) {
        if (depth == remaining) {
            outcomes(0, probability);
            return;
        }
        for (int i = start; i <= type_count - (remaining - depth); ++i) {
            selected[depth] = i;
            subsets(depth + 1, i + 1, probability);
        }
    }
};

EXPORT int projection_mass(
    const double* points, const double* probabilities, const int* offsets,
    const int* sizes, int types, int remaining, double earned, double reference,
    double ideal, TieResolver resolve_tie, double* output
) {
    if (types < 1 || types > 13 || remaining < 1 || remaining > 5 || remaining > types
        || !points || !probabilities || !offsets || !sizes || !output || !resolve_tie)
        return -1;
    for (int i = 0; i < types; ++i)
        if (sizes[i] < 1 || sizes[i] > 16 || offsets[i] < 0) return -2;
    for (int i = 0; i <= 10000; ++i) output[i] = 0;
    std::int64_t combinations = 1;
    for (int i = 0; i < remaining; ++i)
        combinations = combinations * (types - i) / (i + 1);
    Projection p{points, probabilities, offsets, sizes, types, remaining,
        earned, reference, ideal, resolve_tie, output, {}, {}};
    p.subsets(0, 0, 1.0 / static_cast<double>(combinations));
    return 0;
}
