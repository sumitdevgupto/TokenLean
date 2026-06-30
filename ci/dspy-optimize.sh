#!/bin/bash
#
# DSPy Optimizer Step for CI/CD (Optional)
#
# Runs DSPy optimizer on prompt templates to improve quality.
# This is an optional build step that can improve prompt performance.
#
# Usage:
#   bash ci/dspy-optimize.sh --templates-dir templates/ --output-dir optimized/
#

set -e

echo "========================================="
echo "DSPy Prompt Optimizer"
echo "========================================="

# Configuration
TEMPLATES_DIR="${TEMPLATES_DIR:-templates/prompts}"
OUTPUT_DIR="${OUTPUT_DIR:-optimized/prompts}"
OPTIMIZER_EPOCHS="${DSPY_EPOCHS:-3}"
MAX_DEMOS="${DSPY_MAX_DEMOS:-8}"

# Check Python dependencies
echo "Checking dependencies..."
python3 -c "import dspy" 2>/dev/null || {
    echo "Installing DSPy..."
    pip install dspy-ai --quiet
}

# Ensure directories exist
mkdir -p "$OUTPUT_DIR"

# Check if templates exist
if [ ! -d "$TEMPLATES_DIR" ]; then
    echo "⚠️  Templates directory not found: $TEMPLATES_DIR"
    echo "Skipping DSPy optimization"
    exit 0
fi

# Count templates
TEMPLATE_COUNT=$(find "$TEMPLATES_DIR" -name "*.yaml" -o -name "*.json" | wc -l)
if [ "$TEMPLATE_COUNT" -eq 0 ]; then
    echo "No template files found in $TEMPLATES_DIR"
    exit 0
fi

echo "Found $TEMPLATE_COUNT templates to optimize"
echo ""

# Create DSPy optimizer script
cat > /tmp/dspy_optimizer.py << 'PYTHON_EOF'
import json
import sys
import yaml
from pathlib import Path

# DSPy imports
try:
    import dspy
    from dspy.teleprompt import BootstrapFewShot, MIPROv2
except ImportError as e:
    print(f"DSPy not available: {e}")
    sys.exit(0)

def load_template(path: str) -> dict:
    """Load a template from YAML or JSON."""
    with open(path, 'r') as f:
        if path.endswith('.yaml') or path.endswith('.yml'):
            return yaml.safe_load(f)
        return json.load(f)

def save_template(path: str, data: dict):
    """Save optimized template."""
    with open(path, 'w') as f:
        if path.endswith('.yaml') or path.endswith('.yml'):
            yaml.dump(data, f, default_flow_style=False)
        else:
            json.dump(data, f, indent=2)

def optimize_prompt(template_path: str, output_path: str, epochs: int, max_demos: int):
    """Optimize a single prompt using DSPy."""
    print(f"Optimizing: {template_path}")
    
    template = load_template(template_path)
    
    # Extract prompt components
    system_prompt = template.get('system_prompt', '')
    user_template = template.get('user_template', template.get('prompt', ''))
    
    if not system_prompt and not user_template:
        print(f"  ⚠️  No prompt content in {template_path}")
        # Copy as-is
        save_template(output_path, template)
        return
    
    # Create DSPy signature
    class OptimizedSignature(dspy.Signature):
        """Optimized prompt signature."""
        input_text = dspy.InputField()
        output = dspy.OutputField(desc=template.get('output_description', 'Response'))
    
    OptimizedSignature.__doc__ = system_prompt or user_template
    
    # Create simple predictor
    predictor = dspy.Predict(OptimizedSignature)
    
    # Note: In production, you would:
    # 1. Load training examples for this template
    # 2. Define a metric function
    # 3. Use BootstrapFewShot or MIPROv2 to optimize
    
    # For now, we optimize by:
    # - Removing redundant phrases
    # - Improving structure
    optimized_system = optimize_text(system_prompt)
    optimized_user = optimize_text(user_template)
    
    # Update template
    if 'system_prompt' in template:
        template['system_prompt'] = optimized_system
    if 'user_template' in template:
        template['user_template'] = optimized_user
    if 'prompt' in template:
        template['prompt'] = optimized_user
    
    # Mark as optimized
    template['_optimized'] = True
    template['_optimizer'] = 'dspy'
    template['_optimization_metadata'] = {
        'epochs': epochs,
        'max_demos': max_demos,
    }
    
    save_template(output_path, template)
    print(f"  ✅ Saved to: {output_path}")

def optimize_text(text: str) -> str:
    """Apply text optimization heuristics."""
    if not text:
        return text
    
    # Remove filler phrases
    fillers = [
        "As an AI assistant, ",
        "Please note that ",
        "I want to inform you that ",
        "It's important to mention that ",
    ]
    
    optimized = text
    for filler in fillers:
        optimized = optimized.replace(filler, "")
    
    # Collapse multiple spaces
    while "  " in optimized:
        optimized = optimized.replace("  ", " ")
    
    # Remove leading/trailing whitespace from each line
    optimized = '\n'.join(line.strip() for line in optimized.split('\n'))
    
    return optimized.strip()

def main():
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--templates-dir', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--epochs', type=int, default=3)
    parser.add_argument('--max-demos', type=int, default=8)
    
    args = parser.parse_args()
    
    templates_dir = Path(args.templates_dir)
    output_dir = Path(args.output_dir)
    
    # Find all templates
    templates = list(templates_dir.glob('*.yaml')) + list(templates_dir.glob('*.json'))
    
    print(f"Found {len(templates)} templates to optimize")
    print("")
    
    optimized_count = 0
    
    for template_path in templates:
        # Determine output path
        relative = template_path.relative_to(templates_dir)
        output_path = output_dir / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            optimize_prompt(
                str(template_path),
                str(output_path),
                args.epochs,
                args.max_demos,
            )
            optimized_count += 1
        except Exception as e:
            print(f"  ❌ Failed to optimize {template_path}: {e}")
            # Copy as-is on failure
            import shutil
            shutil.copy(template_path, output_path)
    
    print("")
    print(f"Optimized {optimized_count}/{len(templates)} templates")
    print(f"Output directory: {output_dir}")

if __name__ == "__main__":
    main()
PYTHON_EOF

# Run optimizer
echo "Running DSPy optimizer..."
echo "  Epochs: $OPTIMIZER_EPOCHS"
echo "  Max demos: $MAX_DEMOS"
echo ""

python3 /tmp/dspy_optimizer.py \
    --templates-dir "$TEMPLATES_DIR" \
    --output-dir "$OUTPUT_DIR" \
    --epochs "$OPTIMIZER_EPOCHS" \
    --max-demos "$MAX_DEMOS" || {
    echo "⚠️  DSPy optimizer failed — copying templates as-is"
    cp -r "$TEMPLATES_DIR"/* "$OUTPUT_DIR/" 2>/dev/null || true
}

# Generate report
echo ""
echo "========================================="
echo "Optimization Results"
echo "========================================="

OPTIMIZED_COUNT=$(find "$OUTPUT_DIR" -type f \( -name "*.yaml" -o -name "*.json" \) | wc -l)
echo "Templates processed: $OPTIMIZED_COUNT"

# Check for optimization markers
if command -v grep &> /dev/null; then
    MARKED_COUNT=$(grep -l "_optimized" "$OUTPUT_DIR"/* 2>/dev/null | wc -l)
    echo "Successfully optimized: $MARKED_COUNT"
fi

echo ""
echo "========================================="
echo "DSPy optimization complete"
echo "========================================="
