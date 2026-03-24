import pdfplumber
import json
import re
import os
import glob

def build_kuccps_database(output_filename="kuccps_courses.json"):
    print("📄 Starting KUCCPS Master Database Extraction...")
    
    valid_courses = set()
    
    # Regex pattern expanded to catch 'Bachelor of', 'Bachelor in', 'Diploma in', etc.
    course_pattern = re.compile(r'(Bachelor\s+of[^\n]+|Bachelor\s+in[^\n]+|Diploma\s+in[^\n]+|Certificate\s+in[^\n]+|Artisan\s+in[^\n]+)', re.IGNORECASE)

    # Get all PDF files in the current directory (assuming you run this inside the utils/ folder)
    # If you run it from the root backend folder, change "*.pdf" to "utils/*.pdf"
    target_dir = os.path.dirname(__file__) if '__file__' in locals() else '.'
    pdf_files = glob.glob(os.path.join(target_dir, "*.pdf"))

    if not pdf_files:
        print("❌ Error: No PDFs found in the directory!")
        return

    for pdf_filename in pdf_files:
        print(f"\n📂 Opening {os.path.basename(pdf_filename)}...")
        try:
            with pdfplumber.open(pdf_filename) as pdf:
                total_pages = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    if i % 50 == 0:  # Print progress every 50 pages so it doesn't spam your terminal
                        print(f"🔍 Scanning page {i+1} of {total_pages}...")
                    
                    text = page.extract_text()
                    if text:
                        matches = course_pattern.findall(text)
                        for match in matches:
                            # Clean up weird PDF spacing and capitalize properly
                            clean_course = re.sub(r'\s+', ' ', match).strip().title()
                            
                            # Ignore random long paragraph texts that accidentally match
                            if len(clean_course) < 100: 
                                valid_courses.add(clean_course)
        except Exception as e:
            print(f"⚠️ Could not read {pdf_filename}: {e}")

    # Convert to a sorted list
    courses_list = sorted(list(valid_courses))
    
    print(f"\n✅ Master Extraction Complete! Found {len(courses_list)} unique KUCCPS courses.")
    
    # Save the JSON file in the same directory as the script
    output_path = os.path.join(target_dir, output_filename)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(courses_list, f, indent=4)
        
    print(f"💾 Saved successfully to {output_path}")

if __name__ == "__main__":
    build_kuccps_database()