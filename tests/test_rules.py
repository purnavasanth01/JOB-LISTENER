"""Run: python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rules  # noqa: E402


class Roles(unittest.TestCase):
    KEEP = {
        "Software Engineer Intern - Summer 2027": "Software",
        "Full Stack Developer Intern": "Software",
        "Backend Engineering Intern (Payments)": "Software",
        "Frontend Engineer Co-op": "Software",
        "Software QA Developer Intern - 2027": "QA/Automation",
        "Test Automation Intern": "QA/Automation",
        "DevOps Engineer Intern": "Cloud/DevOps",
        "Cloud Infrastructure Intern": "Cloud/DevOps",
        "Data Engineer Intern": "Data",
        "Data Analyst Intern": "Data",
        "Data Scientist Intern, Product Analytics": "Data",
        "Machine Learning Engineer Intern": "AI/ML",
        "AI Engineer Intern": "AI/ML",
        "Forward Deployed Engineering Intern (BS/MS) Summer 2027": "Forward Deployed",
        "Security Engineer Intern": "Security",
        "Cybersecurity Intern": "Security",
        "UX Engineer Intern": "UI/UX",
        "UX Researcher Intern – Summer 2027": "UI/UX",
        "Product Manager Intern": "Product/Program",
        "Technical Program Manager Intern": "Product/Program",
        "Research Scientist Intern": "Research",
        "Technology Summer Analyst - Application Development": "Tech Analyst",
        "2027 Technology Summer Analyst Program": "Tech Analyst",
        "Software Engineer Intern - Marketing Technology": "Software",
        "Software Development Engineer Intern": "Software",
        "Sparse Linear Algebra Libraries Intern - Summer 2027": "Software",
        "Applied Research Intern, Integrated Sensing and Communications (ISAC)": "Research",
        "Research Intern, Quantum Simulation and AI - 2027": "Research",
        "Embedded Software Engineer Intern": "Software",
        "Robotics Software Intern": "Software",
        "GPU Compiler Intern": "Software",
        "Product Manager Intern - Summer 2027": "Product/Program",
    }
    DROP = [
        "AutoCAD Drafting Intern", "Mechanical Engineering Intern", "Embedded Systems Intern",
        "Firmware Engineer Intern", "Robotics Engineer Intern", "ASIC Physical Design Intern - 2027", "DFT Intern - 2027",
        "Autonomy Engineer Intern", "Perf Engineering Intern - 2027",
        "Computer Architecture Intern - 2027", "Quantitative Researcher Intern",
        "Quant Trader Intern", "Sales Engineer Intern", "Solutions Engineer Intern", "IT Support Intern",
        "Systems Administrator Intern", "Business Analyst Intern", "Financial Analyst Intern",
        "Marketing Intern", "MBA Intern - Summer 2027", "Industrial Design Intern - Enterprise (ISG)",
        "2027 Summer Analyst - Investment Banking", "Circuit Validation Engineer Intern",
        "Supply Chain Analyst Intern", "Product Development Intern - Mining",
    ]

    def test_keep(self):
        for title, label in self.KEEP.items():
            got, reason = rules.classify_role(title)
            self.assertEqual(got, label, "%s -> %s (%s)" % (title, got, reason))

    def test_drop(self):
        for title in self.DROP:
            got, reason = rules.classify_role(title)
            self.assertIsNone(got, "%s should drop, got %s" % (title, got))


class Terms(unittest.TestCase):
    def test_terms(self):
        self.assertIsNone(rules.term_check("Software Engineer Intern - Summer 2027"))
        self.assertIsNone(rules.term_check("Software Engineer Co-op (Fall 2027)"))
        self.assertIsNone(rules.term_check("Spring 2027 Co-op - Data"))
        self.assertIsNone(rules.term_check("Software Engineer Intern"))
        self.assertIsNotNone(rules.term_check("Software Engineer Intern - Summer 2026"))
        self.assertIsNotNone(rules.term_check("2028 Summer Intern - SWE"))
        self.assertIsNone(rules.term_check("SWE Intern", terms=["Summer 2027", "Fall 2027"]))
        self.assertIsNone(rules.term_check("SWE Intern", terms=["N/A"]))
        self.assertIsNotNone(rules.term_check("SWE Intern", terms=["Fall 2026"]))
        # graduation years in the description must not drop a job
        self.assertIsNone(rules.term_check("SWE Intern", desc="Candidates graduating in Spring 2028 are preferred."))
        self.assertIsNotNone(rules.term_check("SWE Intern", desc="This internship is for summer 2026 only."))


class Locations(unittest.TestCase):
    def test_locations(self):
        for loc in ["US, TX, Austin", "Santa Clara, CA", "Remote - US", "New York, NY, United States",
                    "Los Gatos,California,United States of America", "2 Locations", "Seattle", "Remote"]:
            self.assertIsNone(rules.location_check([loc]), loc)
        for loc in ["China, Shanghai", "Toronto, ON, Canada", "London, United Kingdom", "Bengaluru, India",
                    "Taiwan, Hsinchu", "Sao Paulo, Sao Paulo, Brazil", "Doha, Qatar"]:
            self.assertIsNotNone(rules.location_check([loc]), loc)
        self.assertIsNone(rules.location_check(["Toronto, Canada", "Austin, TX"]))
        self.assertIsNone(rules.location_check(["Bellevue, Washington; Mountain View, California; Toronto, Canada"]))
        self.assertIsNotNone(rules.location_check(["Berlin, Germany; Amsterdam, Netherlands"]))

    def test_generic_titles(self):
        self.assertEqual(rules.classify_role("Summer 2027 Intern", "Software")[0], "General (check)")
        self.assertIsNone(rules.classify_role("Energy Intern", "AI/ML/Data")[0])
        self.assertIsNone(rules.classify_role("Intern - DRAM Device Engineer")[0])
        self.assertEqual(rules.classify_role("Member of Technical Staff Intern")[0], "Research")


class HardDrops(unittest.TestCase):
    def test_citizenship(self):
        drops = [
            "Applicants must be U.S. citizens.",
            "This position requires U.S. citizenship.",
            "Must be a U.S. citizen or permanent resident due to export control requirements.",
            "Candidates must be a U.S. citizen, permanent resident, refugee, or asylee.",
            "Must be able to obtain and maintain a security clearance.",
            "Active Secret clearance required.",
            "Only U.S. persons are eligible for this role.",
            "Shift: Day. Travel: No. US Citizenship. Clearance Level: None.",
            "Citizenship: U.S. Citizen. Location: Herndon, VA.",
        ]
        for d in drops:
            self.assertIsNotNone(rules.hard_drop("SWE Intern", d), d)
        keeps = [
            "We are an equal opportunity employer and do not discriminate based on citizenship status.",
            "No security clearance is required.",
            "Applicants must be authorized to work in the U.S. We do not sponsor visas.",
            "Some positions may require U.S. citizenship due to export control regulations.",
            "Must be a U.S. citizen, permanent resident, or hold valid work authorization (F-1 CPT welcome).",
            "Great team, fun projects, free lunch.",
        ]
        for d in keeps:
            self.assertIsNone(rules.hard_drop("SWE Intern", d), d)

    def test_phd(self):
        self.assertEqual(rules.hard_drop("Research Scientist Intern, PhD"), "PhD only")
        self.assertEqual(rules.hard_drop("Business Data Scientist Intern Phd Summer 2027"), "PhD only")
        self.assertIsNone(rules.hard_drop("Software Engineer Intern, MS/PhD"))
        self.assertIsNone(rules.hard_drop("Software Engineer Intern"))
        self.assertEqual(rules.hard_drop("ML Intern", degrees=["PhD"]), "PhD only")
        self.assertIsNone(rules.hard_drop("ML Intern", degrees=["Master's", "PhD"]))
        self.assertEqual(rules.hard_drop("ML Intern", "You must be currently enrolled in a PhD program."), "PhD only")
        self.assertIsNone(rules.hard_drop("ML Intern", "Currently enrolled in a PhD or Master's program."))

    def test_list_flag(self):
        self.assertIsNotNone(rules.hard_drop("SWE Intern", sponsorship="U.S. Citizenship is Required"))
        self.assertIsNone(rules.hard_drop("SWE Intern", sponsorship="Does Not Offer Sponsorship"))

    def test_new_drops(self):
        e = rules.check_eligibility
        cases = {
            "This role is subject to ITAR.": "ITAR (US persons only)",
            "Students on an F1 visa (including those with CPT or OPT authorization) are not eligible.": "F-1 / CPT / OPT not eligible",
            "Candidates must be authorized to work without sponsorship now or in the future.": "no sponsorship now or in the future",
            "We will not sponsor future employment visas for this role.": "no sponsorship now or in the future",
            "We do not sponsor H-1B visas.": "no H-1B sponsorship",
            "Must be currently pursuing a Bachelor's degree in Computer Science.": "undergrad only (description)",
            "Open to rising seniors in Computer Science.": "undergrad only (description)",
            "Applicants must have a GPA of 3.0 or above. Must be currently pursuing a Bachelor's degree in CS.": "undergrad only (description)",
            "Candidates must be enrolled in a PhD program.": "PhD required (description)",
        }
        for d, want in cases.items():
            self.assertEqual(e("Software Engineer Intern", d)["drop"], want, d)
        keeps = [
            "We are unable to sponsor visas for this internship.",
            "Currently pursuing a Bachelor's or Master's degree in CS.",
            "Applicants must be authorized to work in the US.",
            "F-1 students are eligible if they are using CPT.",
            "We sponsor H-1B for full-time conversions.",
            "- Are enrolled in a Bachelor's degree or above in Computer Science, Computer Engineering, or related STEM fields",
            "Currently enrolled in a Bachelor's degree program or higher in Computer Science.",
            "Must be pursuing a Bachelor's degree in CS and above.",
        ]
        for d in keeps:
            self.assertIsNone(e("Software Engineer Intern", d)["drop"], d)
        self.assertEqual(e("Software Engineer Intern - Sophomore")["drop"], "undergrad only")
        self.assertEqual(e("Undergraduate Software Engineering Intern")["drop"], "undergrad only")
        self.assertIsNone(e("Undergraduate/Graduate Software Intern")["drop"])
        self.assertEqual(e("Master's ML Intern", "Must be enrolled in a PhD program.")["drop"], "PhD required (description)")

    def test_line_and_flags(self):
        r = rules.check_eligibility("SWE Intern", "Great team. We are unable to sponsor visas for this internship. Free lunch.")
        self.assertEqual(r["line"], "We are unable to sponsor visas for this internship.")
        r = rules.check_eligibility("SWE Intern", "F-1 students are eligible if they are using CPT.")
        self.assertIn("✓ F-1/CPT welcome", r["flags"])
        r = rules.check_eligibility("SWE Intern", "Must be graduating between December 2026 and June 2027.")
        self.assertTrue(any("graduation" in f for f in r["flags"]))
        r = rules.check_eligibility("SWE Intern", "Expected graduation between December 2027 and June 2028.")
        self.assertFalse(any("graduation" in f for f in r["flags"]))
        r = rules.check_eligibility("SWE Intern", "Company H1B Sponsorship. Build APIs.")
        self.assertIsNone(r["line"])
        r = rules.check_eligibility("SWE Intern", None, sponsorship="Does Not Offer Sponsorship")
        self.assertEqual(r["line"], "Listed as: Does Not Offer Sponsorship")

    def test_tech_words(self):
        self.assertGreaterEqual(rules.tech_word_count("Build REST APIs in Python and Java on AWS with Docker."), 4)
        self.assertLess(rules.tech_word_count("Manage store staff and inventory with Excel."), 2)


class Identity(unittest.TestCase):
    def test_company(self):
        self.assertEqual(rules.norm_company("JPMorgan Chase & Co."), rules.norm_company("JPMorgan Chase and Co"))
        self.assertEqual(rules.norm_company("Dell Technologies"), rules.norm_company("Dell"))
        self.assertNotEqual(rules.norm_company("Meta"), rules.norm_company("Metamorphic"))

    def test_url_keys(self):
        a = rules.url_keys("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Software-Intern_JR2005555?utm_source=Simplify")
        b = rules.url_keys("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Software-Intern_JR2005555")
        self.assertIn("wd:nvidia:jr2005555", a & b)
        self.assertIn("gh:8194287", rules.url_keys("https://stripe.com/jobs/search?gh_jid=8194287"))
        self.assertIn("gh:123456", rules.url_keys("https://job-boards.greenhouse.io/stripe/jobs/123456"))
        self.assertIn("wd:capitalone:r249110", rules.url_keys(
            "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/New-York-NY/Current-PhD--AI_R249110-1"))
        self.assertIn("wd:mastercard:r-284880", rules.url_keys(
            "https://mastercard.wd1.myworkdayjobs.com/Campus/job/OFallon-Missouri/Platform-Engineering-Intern_R-284880"))
        self.assertIn("wd:wf:r-123456", rules.url_keys(
            "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Charlotte/Tech-Intern_R-123456"))

    def test_similar(self):
        t = rules.title_tokens
        self.assertTrue(rules.similar_titles(t("Software Engineer Intern - Summer 2027"), t("Software Engineer, Intern (2027)")))
        self.assertFalse(rules.similar_titles(t("Software Engineer Intern"), t("Machine Learning Engineer Intern")))
        self.assertFalse(rules.similar_titles(t("Software Engineer Intern"), t("Software Engineer Intern, Machine Learning")))


if __name__ == "__main__":
    unittest.main()
