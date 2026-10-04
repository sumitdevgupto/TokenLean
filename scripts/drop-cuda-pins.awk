# Drops torch's CUDA companions from a pip-compile lockfile: every nvidia-* and cuda-* pin
# (newer torch reaches the CUDA runtime through cuda-toolkit and cuda-bindings) with its
# "# via" block. Also drops a "# via" block that follows another in the same package's comments:
# the orphan an earlier `sed` sweep left when it removed a pin and kept its block.
#
# The images install torch's CPU build from PyTorch's own index first, so these pins could only
# add the CUDA runtime. Run by scripts/compile-requirements.sh on every lockfile it writes:
#   awk -f scripts/drop-cuda-pins.awk requirements.txt > requirements.txt.tmp
/^(nvidia-|cuda-)/ { drop = 1; next }
/^[^ #]/           { drop = 0; via = 0 }
/^    # via/       { if (via) drop = 1; via = 1 }
drop && /^    #/   { next }
{ print }
